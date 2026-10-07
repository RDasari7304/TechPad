"""Tools the agent can use to actually try a product.

Every tool returns a string. Every tool is defensive: errors become text, never exceptions.
"""
from __future__ import annotations

import base64
import json
import os
import re
import subprocess
import sys
import tempfile
import time
from typing import Any
from urllib.parse import urlparse

import requests

from .config import settings

UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/129.0 Safari/537.36 TechPad/0.1"

PRIVATE_HOSTS = ("localhost", "127.", "10.", "192.168.", "169.254.", "0.0.0.0", "::1")


def _safe_url(url: str) -> str | None:
    try:
        p = urlparse(url if "://" in url else "https://" + url)
    except Exception:
        return None
    if p.scheme not in ("http", "https") or not p.hostname:
        return None
    if os.environ.get("LARPCHECK_ALLOW_LOCAL") != "1" and (p.hostname.startswith(PRIVATE_HOSTS) or p.hostname.endswith(".local")):
        return None
    return p.geturl()


def html_to_text(html: str, limit: int = 12000) -> str:
    try:
        from markdownify import markdownify as md  # type: ignore
        text = md(html, strip=["script", "style", "svg", "noscript"], heading_style="ATX")
    except Exception:
        text = re.sub(r"<(script|style)[^>]*>.*?</\1>", " ", html, flags=re.S | re.I)
        text = re.sub(r"<[^>]+>", " ", text)
    text = re.sub(r"\n\s*\n\s*\n+", "\n\n", text)
    text = re.sub(r"[ \t]{2,}", " ", text).strip()
    return text[:limit] + ("\n...[truncated]" if len(text) > limit else "")


def extract_links(html: str, base: str) -> list[str]:
    links = set()
    for m in re.finditer(r'href=["\']([^"\']+)', html, re.I):
        href = m.group(1).strip()
        if href.startswith("#") or href.startswith("javascript:"):
            continue
        if href.startswith("/"):
            p = urlparse(base)
            href = f"{p.scheme}://{p.netloc}{href}"
        if href.startswith("http"):
            links.add(href.split("#")[0])
    return sorted(links)[:80]


# ----------------------------------------------------------------------------
# HTTP
# ----------------------------------------------------------------------------
def fetch_url(url: str, max_chars: int = 12000, raw: bool = False) -> str:
    u = _safe_url(url)
    if not u:
        return f"ERROR: refusing to fetch {url!r} (invalid or private address)"
    try:
        r = requests.get(u, headers={"User-Agent": UA, "Accept": "*/*"}, timeout=settings.HTTP_TIMEOUT,
                         allow_redirects=True)
    except requests.RequestException as e:
        return f"ERROR: fetch failed: {e}"
    ctype = r.headers.get("content-type", "")
    head = f"HTTP {r.status_code} {r.url}\ncontent-type: {ctype}\n"
    body = r.text
    if raw or "json" in ctype or "text/plain" in ctype:
        return head + body[:max_chars]
    if "html" in ctype:
        links = extract_links(body, r.url)
        return head + html_to_text(body, max_chars) + "\n\nLINKS:\n" + "\n".join(links[:40])
    return head + f"[binary {len(r.content)} bytes]"


def http_request(method: str, url: str, headers: dict | None = None, body: Any = None,
                 json_body: Any = None) -> str:
    """Hit an API endpoint a project claims to expose (GET/POST/PUT/DELETE)."""
    u = _safe_url(url)
    if not u:
        return f"ERROR: refusing to call {url!r}"
    h = {"User-Agent": UA}
    h.update(headers or {})
    try:
        t0 = time.time()
        r = requests.request(method.upper(), u, headers=h, data=body, json=json_body,
                             timeout=settings.HTTP_TIMEOUT)
        dt = time.time() - t0
    except requests.RequestException as e:
        return f"ERROR: request failed: {e}"
    text = r.text[:8000]
    return f"HTTP {r.status_code} in {dt:.2f}s\nheaders: {json.dumps(dict(r.headers))[:1500]}\n\n{text}"


# ----------------------------------------------------------------------------
# GitHub (public API, unauthenticated; add GITHUB_TOKEN env for higher limits)
# ----------------------------------------------------------------------------
def _gh(path: str) -> Any:
    h = {"User-Agent": UA, "Accept": "application/vnd.github+json"}
    tok = os.environ.get("GITHUB_TOKEN")
    if tok:
        h["Authorization"] = f"Bearer {tok}"
    r = requests.get("https://api.github.com" + path, headers=h, timeout=settings.HTTP_TIMEOUT)
    if r.status_code != 200:
        raise RuntimeError(f"github {r.status_code}: {r.text[:200]}")
    return r.json()


def github_inspect(repo: str, path: str = "") -> str:
    """repo = 'owner/name' (or a github URL). Returns repo stats, recent commits, tree and README/file."""
    m = re.search(r"github\.com/([^/\s]+)/([^/\s#?]+)", repo)
    if m:
        repo = f"{m.group(1)}/{m.group(2).removesuffix('.git')}"
    if not re.fullmatch(r"[\w.-]+/[\w.-]+", repo):
        return f"ERROR: bad repo {repo!r}"
    out = []
    try:
        info = _gh(f"/repos/{repo}")
        out.append(f"repo: {info['full_name']} | stars {info['stargazers_count']} | forks {info['forks_count']} | "
                   f"created {info['created_at']} | pushed {info['pushed_at']} | lang {info.get('language')} | "
                   f"fork_of_other={info.get('fork')} | archived={info.get('archived')}\n"
                   f"description: {info.get('description')}")
        commits = _gh(f"/repos/{repo}/commits?per_page=15")
        out.append("recent commits:")
        for c in commits:
            cm = c.get("commit", {})
            out.append(f"  {cm.get('author', {}).get('date', '')[:10]} {cm.get('message', '').splitlines()[0][:90]}")
        default = info.get("default_branch", "main")
        if path:
            f = _gh(f"/repos/{repo}/contents/{path}")
            if isinstance(f, list):
                out.append(f"dir {path}:\n" + "\n".join(f"  {x['type']} {x['path']} ({x.get('size', 0)}b)" for x in f[:100]))
            else:
                content = base64.b64decode(f.get("content", "")).decode("utf-8", "replace")
                out.append(f"file {path}:\n{content[:10000]}")
        else:
            tree = _gh(f"/repos/{repo}/git/trees/{default}?recursive=1")
            items = tree.get("tree", [])
            out.append(f"tree ({len(items)} entries, truncated={tree.get('truncated')}):")
            out.extend(f"  {t['type']} {t['path']} ({t.get('size', 0)}b)" for t in items[:150])
            try:
                readme = _gh(f"/repos/{repo}/readme")
                out.append("README:\n" + base64.b64decode(readme["content"]).decode("utf-8", "replace")[:8000])
            except Exception:
                out.append("README: none")
    except Exception as e:
        out.append(f"ERROR: {e}")
    return "\n".join(out)


# ----------------------------------------------------------------------------
# Solana RPC (read-only)
# ----------------------------------------------------------------------------
def solana_rpc(method: str, params: list | None = None) -> str:
    allowed = {"getAccountInfo", "getBalance", "getTokenSupply", "getTokenLargestAccounts",
               "getSignaturesForAddress", "getTransaction", "getProgramAccounts", "getMultipleAccounts",
               "getTokenAccountsByOwner", "getRecentPrioritizationFees", "getSlot", "getHealth"}
    if method not in allowed:
        return f"ERROR: method {method} not allowed (read-only RPC). Allowed: {sorted(allowed)}"
    try:
        r = requests.post(settings.SOLANA_RPC, json={"jsonrpc": "2.0", "id": 1, "method": method,
                                                     "params": params or []}, timeout=settings.HTTP_TIMEOUT)
        return json.dumps(r.json())[:10000]
    except Exception as e:
        return f"ERROR: rpc failed: {e}"


def token_lookup(ca: str) -> str:
    """Pull metadata for a Solana token from pump.fun and DexScreener."""
    out = {"ca": ca}
    try:
        r = requests.get(f"https://frontend-api-v3.pump.fun/coins/{ca}", headers={"User-Agent": UA},
                         timeout=settings.HTTP_TIMEOUT)
        if r.status_code == 200:
            d = r.json()
            out["pumpfun"] = {k: d.get(k) for k in ("name", "symbol", "description", "twitter", "telegram",
                                                     "website", "created_timestamp", "complete", "creator",
                                                     "usd_market_cap", "image_uri", "metadata_uri")}
    except Exception as e:
        out["pumpfun_error"] = str(e)
    try:
        r = requests.get(f"https://api.dexscreener.com/latest/dex/tokens/{ca}", headers={"User-Agent": UA},
                         timeout=settings.HTTP_TIMEOUT)
        if r.status_code == 200:
            pairs = (r.json() or {}).get("pairs") or []
            if pairs:
                p = pairs[0]
                info = p.get("info") or {}
                out["dexscreener"] = {
                    "name": p.get("baseToken", {}).get("name"),
                    "symbol": p.get("baseToken", {}).get("symbol"),
                    "dex": p.get("dexId"), "pairCreatedAt": p.get("pairCreatedAt"),
                    "websites": [w.get("url") for w in info.get("websites", [])],
                    "socials": [{"type": s.get("type"), "url": s.get("url")} for s in info.get("socials", [])],
                }
    except Exception as e:
        out["dexscreener_error"] = str(e)
    # on-chain mint sanity
    out["mint"] = solana_rpc("getAccountInfo", [ca, {"encoding": "jsonParsed"}])[:1500]
    return json.dumps(out, indent=1)


# ----------------------------------------------------------------------------
# X / Twitter read-only (fxtwitter — no API key needed)
# ----------------------------------------------------------------------------
def x_lookup(url_or_handle: str) -> str:
    """Fetch a tweet (with replies/quote context) or a profile + recent pinned info via fxtwitter."""
    if not settings.FXTWITTER_BASE:
        return "ERROR: fxtwitter disabled"
    s = url_or_handle.strip()
    m = re.search(r"(?:x|twitter)\.com/([A-Za-z0-9_]+)(?:/status/(\d+))?", s)
    if m:
        handle, tweet_id = m.group(1), m.group(2)
    else:
        handle, tweet_id = s.lstrip("@"), None
    path = f"/{handle}/status/{tweet_id}" if tweet_id else f"/{handle}"
    try:
        r = requests.get(settings.FXTWITTER_BASE + path, headers={"User-Agent": UA}, timeout=settings.HTTP_TIMEOUT)
        d = r.json()
    except Exception as e:
        return f"ERROR: x lookup failed: {e}"
    if tweet_id and "tweet" in d:
        t = d["tweet"]
        a = t.get("author", {})
        res = {
            "author": {"name": a.get("name"), "handle": a.get("screen_name"), "followers": a.get("followers"),
                       "joined": a.get("joined"), "description": a.get("description"), "website": a.get("website")},
            "text": t.get("text"), "created": t.get("created_at"), "likes": t.get("likes"),
            "retweets": t.get("retweets"), "replies": t.get("replies"), "views": t.get("views"),
            "urls": re.findall(r"https?://\S+", t.get("text", "")),
            "media": [mm.get("url") for mm in (t.get("media") or {}).get("all", [])],
            "quote": (t.get("quote") or {}).get("text"),
        }
        return json.dumps(res, indent=1)
    if "user" in d:
        u = d["user"]
        res = {k: u.get(k) for k in ("name", "screen_name", "description", "website", "location", "joined",
                                     "followers", "following", "tweets", "verified")}
        return json.dumps(res, indent=1)
    return f"ERROR: fxtwitter returned {json.dumps(d)[:400]}"


# ----------------------------------------------------------------------------
# Browser (Playwright) — one session per agent run
# ----------------------------------------------------------------------------
class Browser:
    def __init__(self, on_frame=None) -> None:
        self._pw = None
        self._browser = None
        self.page = None
        self.console: list[str] = []
        self.network_errors: list[str] = []
        self.on_frame = on_frame  # callback(b64_jpeg, url, title) for the live camera
        self.deadline: float | None = None  # time.time() by which every page action must have finished

    def _ms(self, cap_ms: int) -> int:
        """Playwright timeout that never runs past the deadline."""
        if self.deadline is None:
            return cap_ms
        left = int((self.deadline - time.time()) * 1000) - 500
        if left < 1500:
            raise TimeoutError("time limit reached")
        return min(cap_ms, left)

    def _settle(self, ms: int) -> None:
        """Short wait for the page to render, trimmed so it never runs past the deadline."""
        if self.deadline is not None:
            ms = min(ms, max(0, int((self.deadline - time.time()) * 1000) - 500))
        if ms > 0:
            self.page.wait_for_timeout(ms)

    def _frame(self, label: str = "") -> None:
        if not self.on_frame or not self.page:
            return
        try:
            data = self.page.screenshot(type="jpeg", quality=45)
            self.on_frame(base64.b64encode(data).decode(), self.page.url, self.page.title(), label)
        except Exception:
            pass

    def peek(self, url: str, label: str = "", scrolls: int = 2) -> bool:
        """Visual-only visit for the live camera: open, screenshot, scroll, screenshot. Never raises."""
        u = _safe_url(url)
        if not u:
            return False
        try:
            timeout = self._ms(12000)
            self._ensure()
            self.page.goto(u, wait_until="domcontentloaded", timeout=timeout)
            self._settle(1500)
            self._frame(label)
            for _ in range(scrolls):
                self.page.mouse.wheel(0, 650)
                self._settle(700)
                self._frame(label)
            return True
        except Exception:
            self._frame(label)
            return False

    def _ensure(self) -> None:
        if self.page is not None:
            return
        from playwright.sync_api import sync_playwright  # type: ignore
        self._pw = sync_playwright().start()
        self._browser = self._pw.chromium.launch(headless=True)
        ctx = self._browser.new_context(user_agent=UA, viewport={"width": 1024, "height": 720})
        self.page = ctx.new_page()
        self.page.on("console", lambda m: self.console.append(f"[{m.type}] {m.text}"[:300]))
        self.page.on("requestfailed", lambda r: self.network_errors.append(f"{r.method} {r.url} -> {r.failure}"[:300]))

    def goto(self, url: str) -> str:
        u = _safe_url(url)
        if not u:
            return f"ERROR: refusing {url!r}"
        try:
            timeout = self._ms(20000)
        except TimeoutError:
            return "ERROR: time limit reached, page not opened"
        self._ensure()
        self.console.clear(); self.network_errors.clear()
        try:
            resp = self.page.goto(u, wait_until="domcontentloaded", timeout=timeout)
            self._settle(2000)
        except Exception as e:
            self._frame()
            return f"ERROR: navigation failed: {e}"
        self._frame()
        status = resp.status if resp else "?"
        return f"loaded {self.page.url} (HTTP {status}) title={self.page.title()!r}\n\n{self.snapshot()}"

    def snapshot(self) -> str:
        """Compact, numbered list of interactive elements + visible text."""
        self._ensure()
        js = """
        () => {
          const els = [...document.querySelectorAll('a,button,input,select,textarea,[role=button],[onclick]')];
          const out = [];
          els.forEach((e,i)=>{
            const r = e.getBoundingClientRect(); if (r.width<2||r.height<2) return;
            const label = (e.innerText||e.value||e.placeholder||e.getAttribute('aria-label')||'').trim().slice(0,60);
            const href = e.getAttribute('href')||'';
            out.push(`[${i}] <${e.tagName.toLowerCase()}${e.type?' type='+e.type:''}> ${label} ${href?'-> '+href.slice(0,80):''}`);
            e.setAttribute('data-lc', String(i));
          });
          return out.slice(0,120).join('\\n');
        }"""
        try:
            elements = self.page.evaluate(js)
            text = self.page.evaluate("() => document.body ? document.body.innerText : ''")
        except Exception as e:
            return f"ERROR: snapshot failed: {e}"
        text = re.sub(r"\n{3,}", "\n\n", text)[:6000]
        extra = ""
        if self.console:
            extra += "\nCONSOLE:\n" + "\n".join(self.console[-15:])
        if self.network_errors:
            extra += "\nNETWORK ERRORS:\n" + "\n".join(self.network_errors[-10:])
        return f"ELEMENTS:\n{elements}\n\nTEXT:\n{text}{extra}"

    def act(self, action: str, target: str = "", value: str = "") -> str:
        self._ensure()
        try:
            if action == "click":
                sel = f"[data-lc='{target}']" if target.isdigit() else target
                self.page.click(sel, timeout=self._ms(8000))
            elif action == "type":
                sel = f"[data-lc='{target}']" if target.isdigit() else target
                self.page.fill(sel, value, timeout=self._ms(8000))
            elif action == "press":
                self.page.keyboard.press(value or "Enter")
            elif action == "scroll":
                self.page.mouse.wheel(0, int(value or 800))
            elif action == "wait":
                self._settle(min(int(value or 2000), 15000))
            elif action == "eval":
                return str(self.page.evaluate(value))[:6000]
            else:
                return f"ERROR: unknown action {action}"
            self._settle(1500)
            self._frame()
            return f"ok ({self.page.url})\n\n{self.snapshot()}"
        except Exception as e:
            return f"ERROR: {action} failed: {e}"

    def screenshot(self, path: str) -> None:
        if self.page:
            self.page.screenshot(path=path, full_page=False)

    def close(self) -> None:
        try:
            if self._browser:
                self._browser.close()
            if self._pw:
                self._pw.stop()
        except Exception:
            pass
        self.page = None


# ----------------------------------------------------------------------------
# Code execution (subprocess with timeout) — used to call SDKs / run quick probes
# ----------------------------------------------------------------------------
def run_python(code: str, timeout: int = 60) -> str:
    if not settings.RUN_CODE:
        return "ERROR: code execution disabled (LARPCHECK_RUN_CODE=0)"
    with tempfile.TemporaryDirectory() as d:
        p = os.path.join(d, "probe.py")
        with open(p, "w", encoding="utf-8") as f:
            f.write(code)
        try:
            r = subprocess.run([sys.executable, "-I", p], cwd=d, capture_output=True, text=True,
                               timeout=min(timeout, 180))
            return f"exit={r.returncode}\nSTDOUT:\n{r.stdout[-6000:]}\nSTDERR:\n{r.stderr[-3000:]}"
        except subprocess.TimeoutExpired:
            return f"ERROR: timed out after {timeout}s"


def run_shell(cmd: str, timeout: int = 60) -> str:
    if not settings.RUN_CODE:
        return "ERROR: code execution disabled"
    try:
        r = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=min(timeout, 180))
        return f"exit={r.returncode}\nSTDOUT:\n{r.stdout[-6000:]}\nSTDERR:\n{r.stderr[-3000:]}"
    except subprocess.TimeoutExpired:
        return f"ERROR: timed out after {timeout}s"


# ----------------------------------------------------------------------------
# Tool schemas (what the model sees)
# ----------------------------------------------------------------------------
TOOL_SCHEMAS: list[dict] = [
    {"name": "fetch_url", "description": "Fetch a web page or file as text (HTML is converted to readable text and links are listed). Use for websites, docs, gitbooks, whitepapers, JSON endpoints.",
     "input_schema": {"type": "object", "properties": {"url": {"type": "string"}, "raw": {"type": "boolean", "description": "return raw body instead of text extraction"}}, "required": ["url"]}},
    {"name": "http_request", "description": "Call an HTTP API endpoint the project claims to expose. Returns status, latency, headers and body. Use this to test APIs, bots' webhooks, health endpoints, etc.",
     "input_schema": {"type": "object", "properties": {"method": {"type": "string"}, "url": {"type": "string"}, "headers": {"type": "object"}, "json_body": {}, "body": {"type": "string"}}, "required": ["method", "url"]}},
    {"name": "browser_goto", "description": "Open a URL in a real headless Chromium browser and get a snapshot: numbered interactive elements, visible text, console errors and failed network requests. Use this for dApps, dashboards, 'try it' demos, anything JS-rendered.",
     "input_schema": {"type": "object", "properties": {"url": {"type": "string"}}, "required": ["url"]}},
    {"name": "browser_act", "description": "Interact with the open browser page. action=click|type|press|scroll|wait|eval. target = element number from the snapshot (or a CSS selector). value = text to type / key to press / pixels to scroll / ms to wait / JS to eval.",
     "input_schema": {"type": "object", "properties": {"action": {"type": "string"}, "target": {"type": "string"}, "value": {"type": "string"}}, "required": ["action"]}},
    {"name": "github_inspect", "description": "Inspect a public GitHub repo: stars, commit history, file tree, README; or read a specific file/dir with `path`. Use to judge whether code is real, original, recent, and matches the claims.",
     "input_schema": {"type": "object", "properties": {"repo": {"type": "string", "description": "owner/name or github URL"}, "path": {"type": "string"}}, "required": ["repo"]}},
    {"name": "solana_rpc", "description": "Read-only Solana JSON-RPC call (getAccountInfo, getSignaturesForAddress, getTransaction, getProgramAccounts, getTokenLargestAccounts, ...). Use to verify claimed programs/contracts exist and have activity.",
     "input_schema": {"type": "object", "properties": {"method": {"type": "string"}, "params": {"type": "array"}}, "required": ["method"]}},
    {"name": "x_lookup", "description": "Read a tweet (with author stats) or an X profile without logging in. Accepts a tweet URL, profile URL or @handle.",
     "input_schema": {"type": "object", "properties": {"url_or_handle": {"type": "string"}}, "required": ["url_or_handle"]}},
    {"name": "run_python", "description": "Run a short Python 3 script (requests is available) with a timeout. Use to exercise SDKs, hit APIs with custom logic, decode data, or write small probes. Print what you learn.",
     "input_schema": {"type": "object", "properties": {"code": {"type": "string"}, "timeout": {"type": "integer"}}, "required": ["code"]}},
    {"name": "run_shell", "description": "Run a shell command (curl, git clone, npm/pip install into a temp dir, node, etc.) with a timeout. Use to clone and run claimed open-source code.",
     "input_schema": {"type": "object", "properties": {"cmd": {"type": "string"}, "timeout": {"type": "integer"}}, "required": ["cmd"]}},
]


class ToolBox:
    """Dispatches tool calls for one agent run; owns the browser session."""

    def __init__(self, on_frame=None) -> None:
        self.browser = Browser(on_frame=on_frame)
        self.calls = 0

    def handle(self, name: str, inp: dict) -> str:
        self.calls += 1
        if name == "fetch_url":
            out = fetch_url(inp["url"], raw=bool(inp.get("raw")))
            if settings.USE_BROWSER and "content-type: text/html" in out[:300]:
                self.browser.peek(inp["url"], label="website", scrolls=1)
            return out
        if name == "http_request":
            return http_request(inp.get("method", "GET"), inp["url"], inp.get("headers"), inp.get("body"), inp.get("json_body"))
        if name == "browser_goto":
            if not settings.USE_BROWSER:
                return "ERROR: browser disabled; use fetch_url"
            return self.browser.goto(inp["url"])
        if name == "browser_act":
            if not settings.USE_BROWSER:
                return "ERROR: browser disabled"
            return self.browser.act(inp.get("action", ""), str(inp.get("target", "")), str(inp.get("value", "")))
        if name == "github_inspect":
            return github_inspect(inp["repo"], inp.get("path", ""))
        if name == "solana_rpc":
            return solana_rpc(inp["method"], inp.get("params"))
        if name == "x_lookup":
            return x_lookup(inp["url_or_handle"])
        if name == "run_python":
            return run_python(inp["code"], int(inp.get("timeout") or 60))
        if name == "run_shell":
            return run_shell(inp["cmd"], int(inp.get("timeout") or 60))
        return f"ERROR: unknown tool {name}"

    def close(self) -> None:
        self.browser.close()
