"""Launchpad: audit a tech project, then launch it on pump.fun with the website locked to our coin page.

Non-custodial: the browser generates the mint keypair and signs with the dev's wallet. The server only
(1) runs the audit, (2) uploads metadata to pump.fun's IPFS, (3) asks PumpPortal to build the create tx,
(4) verifies the mint exists on-chain afterwards.
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import time

import requests

from .config import settings
from .db import DB
from .queue import JobManager
from .tools import UA, solana_rpc

BASE58_RE = re.compile(r"^[1-9A-HJ-NP-Za-km-z]{32,44}$")
VIDEO_HOSTS = ("youtube.com", "youtu.be", "x.com", "twitter.com", "vimeo.com", "loom.com", "streamable.com")


class LaunchError(Exception):
    pass


def _save_b64(lid: str, field: str, data_url: str, max_mb: int, allowed: tuple) -> str:
    """Decode a data: URL to data/uploads/<lid>/<field>.<ext>; returns the web path."""
    m = re.match(r"data:([\w/+.-]+);base64,(.+)$", data_url, re.S)
    if not m:
        raise LaunchError(f"{field}: expected a base64 data URL")
    mime, b64 = m.group(1), m.group(2)
    ext = {"image/png": "png", "image/jpeg": "jpg", "image/gif": "gif", "image/webp": "webp",
           "video/mp4": "mp4", "video/webm": "webm", "video/quicktime": "mov"}.get(mime)
    if not ext or mime.split("/")[0] not in allowed:
        raise LaunchError(f"{field}: unsupported type {mime}")
    raw = base64.b64decode(b64)
    if len(raw) > max_mb * 1024 * 1024:
        raise LaunchError(f"{field}: larger than {max_mb} MB")
    d = os.path.join(settings.UPLOAD_DIR, lid)
    os.makedirs(d, exist_ok=True)
    path = os.path.join(d, f"{field}.{ext}")
    with open(path, "wb") as f:
        f.write(raw)
    return f"/uploads/{lid}/{field}.{ext}"


def _clean_url(u: str | None) -> str | None:
    u = (u or "").strip()
    if not u:
        return None
    if not u.startswith("http"):
        u = "https://" + u
    return u[:500]


class Launchpad:
    def __init__(self, db: DB, jobs: JobManager) -> None:
        self.db, self.jobs = db, jobs

    # ---------------------------------------------------------------- submit + audit
    def submit(self, body: dict) -> dict:
        name = (body.get("name") or "").strip()[:32]
        ticker = re.sub(r"[^A-Za-z0-9]", "", body.get("ticker") or "").upper()[:10]
        if not name or not ticker:
            raise LaunchError("name and ticker are required")
        if not body.get("image"):
            raise LaunchError("a coin image is required")
        if not (body.get("description") or "").strip():
            raise LaunchError("describe what the tech does — that is what gets audited")
        wallet = (body.get("creator_wallet") or "").strip()
        if wallet and not BASE58_RE.match(wallet):
            raise LaunchError("creator wallet is not a valid Solana address")
        data = {
            "name": name, "ticker": ticker, "description": (body.get("description") or "").strip()[:2000],
            "github": _clean_url(body.get("github")), "website": _clean_url(body.get("website")),
            "app_url": _clean_url(body.get("app_url")),
            "docs_url": _clean_url(body.get("docs_url")), "x_url": _clean_url(body.get("x_url")),
            "telegram": _clean_url(body.get("telegram")), "video_url": _clean_url(body.get("video_url")),
            "onboarding": (body.get("onboarding") or "").strip()[:8000], "creator_wallet": wallet or None,
        }
        lid = self.db.create_launch(data)
        try:
            img = _save_b64(lid, "image", body["image"], settings.MAX_IMAGE_MB, ("image",))
            vid = _save_b64(lid, "video", body["video"], settings.MAX_VIDEO_MB, ("video",)) if body.get("video") else None
        except LaunchError:
            self.db.update_launch(lid, status="failed")
            raise
        self.db.update_launch(lid, image_path=img, video_path=vid)
        self.start_audit(lid)
        return self.get(lid)

    def update(self, lid: str, body: dict) -> dict:
        """Developer edits the submission after a failed audit; files are replaced only if new ones are sent."""
        L = self.db.get_launch(lid)
        if not L:
            raise LaunchError("launch not found")
        if L["status"] == "launched":
            raise LaunchError("already launched")
        name = (body.get("name") or L["name"] or "").strip()[:32]
        ticker = re.sub(r"[^A-Za-z0-9]", "", body.get("ticker") or L["ticker"] or "").upper()[:10]
        desc = (body.get("description") or "").strip()[:2000]
        if not (name and ticker and desc):
            raise LaunchError("name, ticker and description are required")
        fields = {"name": name, "ticker": ticker, "description": desc,
                  "github": _clean_url(body.get("github")), "website": _clean_url(body.get("website")),
                  "app_url": _clean_url(body.get("app_url")),
                  "docs_url": _clean_url(body.get("docs_url")), "x_url": _clean_url(body.get("x_url")),
                  "telegram": _clean_url(body.get("telegram")), "video_url": _clean_url(body.get("video_url")),
                  "onboarding": (body.get("onboarding") or "").strip()[:8000]}
        if body.get("image"):
            fields["image_path"] = _save_b64(lid, "image", body["image"], settings.MAX_IMAGE_MB, ("image",))
        if body.get("video"):
            fields["video_path"] = _save_b64(lid, "video", body["video"], settings.MAX_VIDEO_MB, ("video",))
        self.db.update_launch(lid, **fields)
        self.start_audit(lid)
        return self.get(lid)

    def _audit_text(self, L: dict) -> str:
        parts = [f"LAUNCHPAD PRE-LAUNCH AUDIT for '{L['name']}' (${L['ticker']}).",
                 "The developer submitted this project to be launched on pump.fun through TechPad. The developer's own "
                 "description is a CLAIM, not evidence, so try everything that can be tried. Be fair, not harsh: the score "
                 "is the probability the project is real. A repo that runs, an app that loads and does something, or a demo "
                 "that matches the description earn real credit even if some claims stay unverified. Only penalise what is "
                 "actually missing, broken or contradicted. In would_change_verdict, give concrete, specific fixes the "
                 "developer can act on (which link to add, which feature to make reachable, which claim to demo).",
                 f"DESCRIPTION: {L.get('description') or '(none)'}"]
        for k, label in (("github", "GitHub"), ("website", "Website"), ("app_url", "App/demo"), ("docs_url", "Docs"), ("x_url", "X"),
                         ("telegram", "Telegram"), ("video_url", "Demo video")):
            if L.get(k):
                parts.append(f"{label}: {L[k]}")
        if L.get("onboarding"):
            parts.append("ONBOARDING NOTES FROM DEVELOPER:\n" + L["onboarding"])
        return "\n".join(parts)

    def start_audit(self, lid: str) -> dict:
        L = self.db.get_launch(lid)
        if not L:
            raise LaunchError("launch not found")
        if L["status"] == "launched":
            raise LaunchError("already launched")
        self.db.update_launch(lid, status="auditing")

        def on_done(row: dict) -> None:
            self.db.update_launch(lid, status="audited", audit_test_id=row["id"])

        sub = self.jobs.submit(self._audit_text(L), source="launchpad", requester=L.get("creator_wallet") or "",
                               force=True, x_url=L.get("x_url"), key=f"launch:{lid}", on_done=on_done)
        self.db.update_launch(lid, audit_test_id=sub["id"])
        return sub

    # ---------------------------------------------------------------- read
    def get(self, lid: str) -> dict:
        L = self.db.get_launch(lid)
        if not L:
            raise LaunchError("launch not found")
        return self._decorate(L)

    def _decorate(self, L: dict) -> dict:
        audit = None
        if L.get("audit_test_id"):
            row = self.db.get(L["audit_test_id"], full=True)
            if row:
                res = row.get("result") or {}
                v = res.get("verdict") or {}
                audit = {"test_id": row["id"], "status": row["status"], "verdict": v.get("verdict"),
                         "score": v.get("score"), "confidence": v.get("confidence"), "headline": v.get("headline"),
                         "summary": v.get("summary"), "one_liner": (res.get("understanding") or {}).get("one_liner"),
                         "category": (res.get("understanding") or {}).get("category"),
                         "what_works": v.get("what_works"), "red_flags": v.get("red_flags"),
                         "what_doesnt": v.get("what_doesnt"), "would_change_verdict": v.get("would_change_verdict"),
                         "dead_links": res.get("dead_links") or [], "error": res.get("error"),
                         "finished_at": row.get("finished_at")}
        L["audit"] = audit
        L["eligible"], L["eligibility_reason"] = self._eligible(audit)
        L["coin_url"] = f"{settings.PUBLIC_URL}/coin/{L['mint']}" if L.get("mint") else None
        L["pump_url"] = f"https://pump.fun/coin/{L['mint']}" if L.get("mint") else None
        L["certificate"] = self.certificate(L) if L.get("status") == "launched" else None
        return L

    def _eligible(self, audit: dict | None) -> tuple[bool, str]:
        if not audit or audit.get("status") != "done":
            return False, "audit not finished"
        if audit.get("verdict") not in settings.LAUNCH_ALLOWED_VERDICTS:
            return False, f"verdict {audit.get('verdict')} is not launchable (need {' or '.join(settings.LAUNCH_ALLOWED_VERDICTS)})"
        if (audit.get("score") or 0) < settings.LAUNCH_MIN_SCORE:
            return False, f"the agent is only {audit.get('score')}% confident this is real (need {settings.LAUNCH_MIN_SCORE}%)"
        return True, "eligible"

    def certificate(self, L: dict) -> dict:
        a = L.get("audit") or {}
        raw = f"{L['id']}|{L.get('mint')}|{a.get('test_id')}|{a.get('score')}|{a.get('verdict')}"
        return {"id": hashlib.sha256(raw.encode()).hexdigest()[:16].upper(), "verdict": a.get("verdict"),
                "score": a.get("score"), "audited_at": a.get("finished_at"), "report_url":
                f"{settings.PUBLIC_URL}/#t/{a.get('test_id')}", "badge_url": f"{settings.PUBLIC_URL}/badge/{L.get('mint')}.svg"}

    # ---------------------------------------------------------------- deploy
    def prepare(self, lid: str, wallet: str, mint: str, dev_buy_sol: float = 0.0, slippage: int = 10,
                priority_fee: float = 0.0005) -> dict:
        """Upload metadata (website locked to our coin page) and get an unsigned create tx from PumpPortal."""
        L = self.get(lid)
        if L["status"] == "launched":
            raise LaunchError("already launched")
        if not L["eligible"]:
            raise LaunchError(f"not eligible: {L['eligibility_reason']}")
        if not (BASE58_RE.match(wallet or "") and BASE58_RE.match(mint or "")):
            raise LaunchError("wallet and mint must be valid Solana public keys")
        coin_url = f"{settings.PUBLIC_URL}/coin/{mint}"
        img_path = os.path.join(settings.UPLOAD_DIR, *L["image_path"].split("/")[2:])
        with open(img_path, "rb") as f:
            files = {"file": (os.path.basename(img_path), f, "image/" + img_path.rsplit(".", 1)[-1].replace("jpg", "jpeg"))}
            form = {"name": L["name"], "symbol": L["ticker"],
                    "description": (L.get("description") or "")[:900] + f"\n\n✅ Audited by TechPad — {coin_url}",
                    "twitter": L.get("x_url") or "", "telegram": L.get("telegram") or "",
                    "website": coin_url, "showName": "true"}
            r = requests.post(settings.PUMP_IPFS_URL, data=form, files=files, headers={"User-Agent": UA}, timeout=60)
        if r.status_code != 200:
            raise LaunchError(f"pump.fun metadata upload failed ({r.status_code}): {r.text[:200]}")
        meta_uri = r.json().get("metadataUri")
        if not meta_uri:
            raise LaunchError("pump.fun did not return a metadata URI")
        body = {"publicKey": wallet, "action": "create", "mint": mint,
                "tokenMetadata": {"name": L["name"], "symbol": L["ticker"], "uri": meta_uri},
                "denominatedInSol": "true", "amount": float(dev_buy_sol or 0), "slippage": int(slippage),
                "priorityFee": float(priority_fee), "pool": "pump"}
        r = requests.post(settings.PUMPPORTAL_URL, json=body, headers={"User-Agent": UA, "Content-Type": "application/json"},
                          timeout=60)
        if r.status_code != 200:
            raise LaunchError(f"PumpPortal create failed ({r.status_code}): {r.text[:200]}")
        self.db.update_launch(lid, metadata_uri=meta_uri, mint=mint, creator_wallet=wallet)
        return {"tx_base64": base64.b64encode(r.content).decode(), "metadata_uri": meta_uri, "mint": mint,
                "coin_url": coin_url}

    def confirm(self, lid: str, signature: str, mint: str) -> dict:
        """After the browser sent the tx: verify the mint exists on-chain, then flip to launched."""
        L = self.get(lid)
        if L.get("mint") and L["mint"] != mint:
            raise LaunchError("mint does not match the prepared launch")
        ok = False
        for _ in range(12):  # ~1 minute
            try:
                info = json.loads(solana_rpc("getAccountInfo", [mint, {"encoding": "jsonParsed"}]))
                if (info.get("result") or {}).get("value"):
                    ok = True
                    break
            except Exception:
                pass
            time.sleep(5)
        if not ok:
            raise LaunchError("mint not visible on-chain yet — wait a bit and press confirm again")
        self.db.update_launch(lid, status="launched", mint=mint, signature=signature, launched_at=time.time())
        if L.get("audit_test_id"):  # make the archive find the audit by CA
            self.db.rekey_test(L["audit_test_id"], mint, mint)
        return self.get(lid)

    # ---------------------------------------------------------------- public coin page data
    def coin(self, mint: str) -> dict | None:
        L = self.db.launch_by_mint(mint)
        if not L:
            return None
        L = self._decorate(L)
        L["video_embed"] = embed_url(L.get("video_url"))
        return L


def embed_url(u: str | None) -> str | None:
    if not u:
        return None
    m = re.search(r"(?:youtube\.com/watch\?v=|youtu\.be/|youtube\.com/shorts/)([\w-]{6,})", u)
    if m:
        return f"https://www.youtube.com/embed/{m.group(1)}"
    m = re.search(r"vimeo\.com/(\d+)", u)
    if m:
        return f"https://player.vimeo.com/video/{m.group(1)}"
    m = re.search(r"loom\.com/share/([\w-]+)", u)
    if m:
        return f"https://www.loom.com/embed/{m.group(1)}"
    return None


def badge_svg(L: dict | None) -> str:
    v = ((L or {}).get("audit") or {}).get("verdict") or "UNAUDITED"
    score = ((L or {}).get("audit") or {}).get("score")
    col = {"WORKS": "#22c55e", "PARTIAL": "#f59e0b", "UNVERIFIED": "#eab308", "LARP": "#ef4444"}.get(v, "#6b7280")
    label = f"{v} · {score}/100" if score is not None else v
    w = 150 + len(label) * 7
    return f'''<svg xmlns="http://www.w3.org/2000/svg" width="{w}" height="28" role="img" aria-label="TechPad audit: {label}">
<rect rx="6" width="{w}" height="28" fill="#0b0d10"/><rect x="118" rx="6" width="{w-118}" height="28" fill="{col}"/>
<rect x="118" width="6" height="28" fill="{col}"/>
<text x="12" y="18" font-family="ui-monospace,Menlo,monospace" font-size="12" fill="#86efac" font-weight="700">🧪 TECHPAD</text>
<text x="128" y="18" font-family="ui-monospace,Menlo,monospace" font-size="12" fill="#06130c" font-weight="700">{label}</text></svg>'''
