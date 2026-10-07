"""X (Twitter) bot: polls @mentions, runs a test on the tweet being replied to, replies with the verdict.

Needs an X developer app with Read+Write user auth (OAuth 1.0a keys) in .env. Mentions are read with the
bearer token; replies are posted with OAuth 1.0a (implemented here with the stdlib, no extra packages).
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import re
import secrets
import time
from urllib.parse import quote

import requests

from .config import settings
from .db import DB
from .queue import JobManager
from .research import BASE58_RE

API = "https://api.twitter.com/2"
TRIGGER_RE = re.compile(r"check|test|larp|real|works|legit|verify|fake", re.I)


class OAuth1:
    def __init__(self, ck: str, cs: str, at: str, ats: str) -> None:
        self.ck, self.cs, self.at, self.ats = ck, cs, at, ats

    def header(self, method: str, url: str) -> str:
        p = {"oauth_consumer_key": self.ck, "oauth_nonce": secrets.token_hex(16),
             "oauth_signature_method": "HMAC-SHA1", "oauth_timestamp": str(int(time.time())),
             "oauth_token": self.at, "oauth_version": "1.0"}
        base_params = "&".join(f"{quote(k, safe='')}={quote(v, safe='')}" for k, v in sorted(p.items()))
        base = f"{method.upper()}&{quote(url, safe='')}&{quote(base_params, safe='')}"
        key = f"{quote(self.cs, safe='')}&{quote(self.ats, safe='')}"
        p["oauth_signature"] = base64.b64encode(hmac.new(key.encode(), base.encode(), hashlib.sha1).digest()).decode()
        return "OAuth " + ", ".join(f'{quote(k, safe="")}="{quote(v, safe="")}"' for k, v in sorted(p.items()))


class XBot:
    def __init__(self, db: DB, jobs: JobManager) -> None:
        if not settings.X_BEARER_TOKEN:
            raise RuntimeError("X_BEARER_TOKEN not set")
        self.db, self.jobs = db, jobs
        self.bearer = {"Authorization": f"Bearer {settings.X_BEARER_TOKEN}"}
        self.oauth = OAuth1(settings.X_API_KEY, settings.X_API_SECRET, settings.X_ACCESS_TOKEN, settings.X_ACCESS_SECRET)
        me = requests.get(f"{API}/users/by/username/{settings.X_BOT_HANDLE}", headers=self.bearer, timeout=20).json()
        self.user_id = me["data"]["id"]
        print(f"[xbot] running as @{settings.X_BOT_HANDLE} ({self.user_id}); polling every {settings.X_POLL_SECONDS}s")

    # ---- X API -----------------------------------------------------------------
    def mentions(self) -> list[dict]:
        params = {"max_results": 50, "tweet.fields": "author_id,conversation_id,referenced_tweets,entities,created_at",
                  "expansions": "referenced_tweets.id,referenced_tweets.id.author_id",
                  "user.fields": "username"}
        since = self.db.get_state("since_id")
        if since:
            params["since_id"] = since
        r = requests.get(f"{API}/users/{self.user_id}/mentions", headers=self.bearer, params=params, timeout=30)
        if r.status_code != 200:
            print(f"[xbot] mentions {r.status_code}: {r.text[:200]}")
            return []
        d = r.json()
        data = d.get("data", [])
        inc = {t["id"]: t for t in d.get("includes", {}).get("tweets", [])}
        users = {u["id"]: u for u in d.get("includes", {}).get("users", [])}
        if d.get("meta", {}).get("newest_id"):
            self.db.set_state("since_id", d["meta"]["newest_id"])
        out = []
        for t in data:
            parent = None
            for ref in t.get("referenced_tweets", []) or []:
                if ref["type"] in ("replied_to", "quoted") and ref["id"] in inc:
                    parent = inc[ref["id"]]
            out.append({"tweet": t, "parent": parent, "users": users})
        return out

    def reply(self, text: str, in_reply_to: str) -> bool:
        url = f"{API}/tweets"
        r = requests.post(url, headers={"Authorization": self.oauth.header("POST", url), "Content-Type": "application/json"},
                          json={"text": text[:275], "reply": {"in_reply_to_tweet_id": in_reply_to}}, timeout=30)
        if r.status_code not in (200, 201):
            print(f"[xbot] reply failed {r.status_code}: {r.text[:200]}")
            return False
        return True

    # ---- logic ---------------------------------------------------------------------
    @staticmethod
    def _urls(t: dict) -> list[str]:
        return [u.get("expanded_url") or u.get("url") for u in (t.get("entities") or {}).get("urls", [])]

    def handle(self, m: dict) -> None:
        t, parent, users = m["tweet"], m["parent"], m["users"]
        if t.get("author_id") == self.user_id:
            return
        text = t.get("text", "")
        if not TRIGGER_RE.search(text):
            return
        # Build the request: CA from mention or parent, X link = parent tweet (the project's post).
        blob = text + " " + (parent or {}).get("text", "") + " " + " ".join(self._urls(t) + self._urls(parent or {}))
        cas = [c for c in BASE58_RE.findall(blob) if not c.isdigit()]
        x_url = None
        if parent:
            author = users.get(parent.get("author_id"), {}).get("username", "i")
            x_url = f"https://x.com/{author}/status/{parent['id']}"
        if not cas and not x_url and not self._urls(t):
            self.reply("Reply to a project's tweet with @%s check, or include the CA / a link. 🧪" % settings.X_BOT_HANDLE, t["id"])
            return
        req_text = f"{text}\n{blob}"
        print(f"[xbot] test requested by {t.get('author_id')} ca={cas[:1]} x={x_url}")

        def on_done(row: dict) -> None:
            res = (row or {}).get("result") or {}
            v = res.get("verdict") or {}
            url = f"{settings.PUBLIC_URL}/#t/{row['id']}"
            tweet = (v.get("tweet") or f"{v.get('verdict', 'UNVERIFIED')} — {v.get('headline', '')}").replace("{url}", url)
            if "{url}" not in (v.get("tweet") or "") and url not in tweet:
                tweet = f"{tweet[:240]} {url}"
            self.reply(tweet, t["id"])

        self.jobs.submit(req_text, source="x", requester=t.get("author_id", ""), ca=cas[0] if cas else None,
                         x_url=x_url, on_done=on_done)

    def run_forever(self) -> None:
        while True:
            try:
                for m in self.mentions():
                    try:
                        self.handle(m)
                    except Exception as e:
                        print(f"[xbot] handle error: {e}")
            except Exception as e:
                print(f"[xbot] poll error: {e}")
            time.sleep(settings.X_POLL_SECONDS)
