"""Phase 0: parse the user's request and build a dossier about the project before the agent starts."""
from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass, field

from . import tools

BASE58_RE = re.compile(r"\b[1-9A-HJ-NP-Za-km-z]{32,44}\b")
URL_RE = re.compile(r"https?://[^\s<>\"')\]\\]+")
X_RE = re.compile(r"https?://(?:www\.)?(?:x|twitter)\.com/([A-Za-z0-9_]+)(?:/status/(\d+))?", re.I)


@dataclass
class Request:
    ca: str | None = None
    x_url: str | None = None          # tweet or profile URL
    x_handle: str | None = None
    other_urls: list[str] = field(default_factory=list)
    text: str = ""

    def key(self) -> str:
        """Dedupe key: the CA if present, else the X handle, else the first URL."""
        return (self.ca or (self.x_handle and "@" + self.x_handle.lower()) or
                (self.other_urls[0] if self.other_urls else self.text[:80])).strip()


def parse_request(text: str, ca: str | None = None, x_url: str | None = None) -> Request:
    req = Request(text=text.strip())
    urls = URL_RE.findall(text)
    for u in urls:
        m = X_RE.match(u)
        if m and not req.x_url:
            req.x_url, req.x_handle = u, m.group(1)
        elif not m:
            req.other_urls.append(u.rstrip(".,"))
    if x_url:
        req.x_url = x_url
        m = X_RE.match(x_url)
        req.x_handle = m.group(1) if m else None
    cas = [c for c in BASE58_RE.findall(text) if not c.startswith("http")]
    # filter obvious non-CAs (tweet ids are digits; handles are short)
    cas = [c for c in cas if not c.isdigit()]
    req.ca = ca or (cas[0] if cas else None)
    return req


def _discover(text: str) -> dict[str, list[str]]:
    found = {"github": [], "docs": [], "apps": [], "telegram": [], "x": []}
    for u in set(URL_RE.findall(text)):
        lu = u.lower()
        if "github.com" in lu:
            found["github"].append(u)
        elif "t.me/" in lu:
            found["telegram"].append(u)
        elif "x.com" in lu or "twitter.com" in lu:
            found["x"].append(u)
        elif any(k in lu for k in ("docs.", "gitbook", "/docs", "whitepaper", "notion.site", "medium.com")):
            found["docs"].append(u)
        elif not any(k in lu for k in ("pump.fun", "dexscreener", "solscan", "birdeye", "jup.ag", "raydium")):
            found["apps"].append(u)
    return {k: sorted(set(v))[:10] for k, v in found.items()}


def _is_dead(page: str) -> str | None:
    """Return a short reason if a fetched page looks dead/unreachable, else None."""
    head = page[:200]
    if head.startswith("ERROR"):
        return head.splitlines()[0][:160]
    m = re.match(r"HTTP (\d{3})", head)
    if m and int(m.group(1)) >= 400:
        return f"HTTP {m.group(1)}"
    low = page[:3000].lower()
    for sig in ("this domain is parked", "domain for sale", "buy this domain", "site can't be reached",
                "404 not found", "coming soon", "under construction", "account suspended"):
        if sig in low:
            return f"placeholder page ({sig})"
    return None


def build_dossier(req: Request, log=print, on_tool=None, deadline: float | None = None) -> dict:
    """Collect everything we can find before spending LLM tokens. Pure fetching, no LLM.
    Nothing here raises: a dead site becomes a 'dead_links' entry (a red flag), and we move on.
    Past `deadline` (time.time()) the remaining fetches are skipped."""
    d: dict = {"request": req.text, "ca": req.ca, "x_url": req.x_url, "sources": {}, "dead_links": []}
    blob = req.text + "\n"

    def late() -> bool:
        return deadline is not None and time.time() >= deadline

    if req.ca:
        log("looking up token metadata")
        tok = tools.token_lookup(req.ca)
        if on_tool:
            on_tool("token_lookup", {"ca": req.ca}, tok)
        d["sources"]["token"] = tok
        blob += tok + "\n"
        try:
            t = json.loads(tok)
            pf = t.get("pumpfun") or {}
            ds = t.get("dexscreener") or {}
            d["name"] = pf.get("name") or ds.get("name")
            d["symbol"] = pf.get("symbol") or ds.get("symbol")
            for u in [pf.get("twitter"), pf.get("website"), pf.get("telegram")] + (ds.get("websites") or []) + \
                     [s.get("url") for s in (ds.get("socials") or [])]:
                if u:
                    blob += u + "\n"
                    if not req.x_url and X_RE.match(u):
                        req.x_url, req.x_handle = u, X_RE.match(u).group(1)
                        d["x_url"] = u
        except Exception:
            pass

    if req.x_url and not late():
        log(f"reading X: {req.x_url}")
        x = tools.x_lookup(req.x_url)
        if on_tool:
            on_tool("x_lookup", {"url_or_handle": req.x_url}, x)
        if x.startswith("ERROR"):
            d["dead_links"].append({"url": req.x_url, "why": "X account/tweet not found or unreadable"})
        d["sources"]["x"] = x
        blob += x + "\n"
        if "/status/" in req.x_url and req.x_handle and not late():
            prof = tools.x_lookup("@" + req.x_handle)
            if on_tool:
                on_tool("x_lookup", {"url_or_handle": "@" + req.x_handle}, prof)
            d["sources"]["x_profile"] = prof
            blob += prof + "\n"

    links = _discover(blob)
    for u in req.other_urls:
        if u not in sum(links.values(), []):
            links["apps"].append(u)
    d["links"] = links

    for u in (links["apps"] + links["docs"])[:3]:
        if late():
            log("research time is up — moving on to testing")
            break
        log(f"fetching {u}")
        page = tools.fetch_url(u, max_chars=8000)
        if on_tool:
            on_tool("fetch_url", {"url": u}, page)
        why = _is_dead(page)
        if why:
            d["dead_links"].append({"url": u, "why": why})
            log(f"  ✗ unreachable ({why}) — noted as a red flag")
        d["sources"][u] = page
        blob += page + "\n"
    # second-pass discovery (website often links github/docs/app)
    links2 = _discover(blob)
    for k in links:
        links[k] = sorted(set(links[k] + links2[k]))[:10]
    for g in links["github"][:2]:
        if late():
            break
        log(f"inspecting {g}")
        d["sources"][g] = tools.github_inspect(g)
        if on_tool:
            on_tool("github_inspect", {"repo": g}, d["sources"][g])
        if d["sources"][g].startswith("ERROR") or "github 404" in d["sources"][g]:
            d["dead_links"].append({"url": g, "why": "GitHub repo missing or private"})
    d["name"] = d.get("name") or (req.x_handle or "unknown")
    return d


X_EMBED = "https://platform.twitter.com/embed/Tweet.html?dnt=true&id={id}"
X_PROFILE_EMBED = "https://syndication.twitter.com/srv/timeline-profile/screen-name/{handle}"


def camera_tour(browser, req: Request, d: dict, log=print, deadline: float | None = None) -> None:
    """Visit the real pages in the headless browser so the live cam shows them. Visual only; never raises.
    Stops at `deadline` (time.time())."""
    stops: list[tuple[str, str]] = []
    if req.x_url:
        m = X_RE.match(req.x_url)
        if m and m.group(2):
            stops.append((X_EMBED.format(id=m.group(2)), "x.com tweet"))
        if req.x_handle:
            stops.append((X_PROFILE_EMBED.format(handle=req.x_handle), f"x.com/@{req.x_handle}"))
    if req.ca:
        stops.append((f"https://pump.fun/coin/{req.ca}", "pump.fun"))
        stops.append((f"https://dexscreener.com/solana/{req.ca}", "dexscreener"))
    links = d.get("links") or {}
    stops += [(u, "website") for u in (links.get("apps") or [])[:2]]
    stops += [(u, "docs") for u in (links.get("docs") or [])[:1]]
    stops += [(u, "github") for u in (links.get("github") or [])[:1]]
    for url, label in stops[:8]:
        if deadline is not None and deadline - time.time() < 3:
            break
        log(f"📷 {label}: {url}")
        try:
            browser.peek(url, label=label, scrolls=1 if label in ("x.com tweet", "pump.fun") else 2)
        except Exception:
            pass


def dossier_text(d: dict, limit: int = 45000) -> str:
    parts = [f"USER REQUEST: {d['request']}", f"CA: {d.get('ca')}", f"X: {d.get('x_url')}",
             f"NAME/SYMBOL: {d.get('name')} / {d.get('symbol')}", f"DISCOVERED LINKS: {json.dumps(d.get('links'))}"]
    if d.get("dead_links"):
        parts.append("DEAD / UNREACHABLE LINKS (the project advertises these but they do not work — a red flag):\n" +
                     "\n".join(f"  - {x['url']}: {x['why']}" for x in d["dead_links"]))
    for k, v in d["sources"].items():
        parts.append(f"\n===== SOURCE: {k} =====\n{str(v)[:9000]}")
    text = "\n".join(parts)
    return text[:limit]
