"""The LarpCheck agent: understand → test → verdict.

run_test(text) is synchronous and self-contained; the queue runs many of these in parallel threads.
"""
from __future__ import annotations

import json
import os
import time
import traceback
from typing import Callable

from .config import settings
from .llm import LLM, BudgetExceeded, parse_json
from .research import build_dossier, camera_tour, dossier_text, parse_request
from .tools import TOOL_SCHEMAS, ToolBox

VERDICTS = ("WORKS", "PARTIAL", "UNVERIFIED", "LARP")

UNDERSTAND_SYSTEM = """You are TechPad, a skeptical but fair technical due-diligence analyst for Solana "tech" tokens.
You will be given raw material about a project (token metadata, tweets, website text, GitHub). Figure out WHAT the
project claims to be and WHAT it claims to do, then turn those claims into concrete, falsifiable tests.

Rules:
- Only list claims the project itself makes (or strongly implies). Do not invent features.
- Vague projects get vague claims — say so. A project with no product link and only hype has few testable claims.
- For each claim, say how a tester with a browser, HTTP client, Python, shell, GitHub access and read-only Solana RPC
  could actually try it. Be specific (URL to open, endpoint to hit, repo to run, program ID to check).
- Mark claims that are unfalsifiable (future roadmap, partnerships, "AI-powered" with no demo) as testable=false.
- Links the project advertises that are dead, parked, 403/404, SSL-broken or "coming soon" are red flags: list them in
  initial_red_flags and do NOT create testable claims that depend on them (they are already proven unreachable).
Output ONLY JSON:
{
 "project_name": str, "category": str (e.g. "AI agent", "trading bot", "DeFi protocol", "infra/API", "game", "DePIN", "memecoin w/ utility", "unknown"),
 "one_liner": str,
 "detail_level": "none"|"vague"|"moderate"|"extensive",
 "product_links": {"app": str|null, "docs": str|null, "github": str|null, "api": str|null, "telegram": str|null},
 "claims": [{"id": "C1", "claim": str, "source": str, "testable": bool, "how_to_test": str, "importance": "core"|"secondary"}],
 "initial_red_flags": [str]
}"""

TEST_SYSTEM = """You are TechPad's hands-on tester. Your job: actually TRY the product and find out if it works.
You have tools: a real headless browser, HTTP client, GitHub inspector, read-only Solana RPC, Python and shell.

Method:
1. Start with the CORE claims. Open the app / hit the API / clone and run the repo. Don't just read marketing pages — interact.
2. For each claim gather EVIDENCE: what you did, what happened (status codes, UI responses, console errors, outputs).
3. Distinguish: WORKS (you observed it functioning), BROKEN (you tried, it failed/errored/placeholder), UNTESTABLE
   (no way to try it: paywalled, needs invite, future roadmap, no link), FAKE (evidence it's a facade: template site,
   copied repo, fake API, dead links, plagiarized code, screenshot-only "demo").
4. Look for larp signals: Lovable/Framer template with no backend calls, "coming soon" buttons, GitHub repo that is a
   fork with zero changes or only README, endpoints that return static JSON, claimed program IDs that don't exist
   on-chain, metrics that don't change, AI "agents" that are just a chat widget calling nothing.
5. Be efficient: ~{max_calls} tool calls max. Prefer actions that settle a claim fast. If something needs login,
   try the public parts, then move on.
6. Never pay money, never submit private keys, never sign transactions. Wallet connect flows: observe the UI only.
7. If a claim can only be checked by reading code, read the actual code paths, not the README.

When done, output ONLY JSON:
{
 "claim_results": [{"id": "C1", "status": "WORKS"|"BROKEN"|"UNTESTABLE"|"FAKE"|"PARTIAL", "evidence": str, "steps": [str]}],
 "observations": [str],
 "larp_signals": [str],
 "legit_signals": [str]
}"""

VERDICT_SYSTEM = """You are TechPad's judge. Given the claims and hands-on test results, decide if this project is real.

Verdict scale:
- WORKS: core claims demonstrably function.
- PARTIAL: something real exists but key claims are missing, broken or overstated.
- UNVERIFIED: could not be tested meaningfully (no product to try). Say exactly why, and what WOULD prove it.
- LARP: evidence the product is fake, a facade, or the claims are materially false.

Score 0-100 = probability the project does what it says (not price/market quality).
Dead or placeholder links that the project itself advertises count AGAINST it (a real product has a reachable site);
treat them as evidence, lower the score, and name them in red_flags — but a single flaky link alone is not proof of LARP.
Be concrete and quote evidence. Be fair: "untestable" is not the same as "fake".
Output ONLY JSON:
{"verdict": "WORKS"|"PARTIAL"|"UNVERIFIED"|"LARP", "score": int, "confidence": int,
 "headline": str (<=100 chars), "summary": str (3-6 sentences, plain English, for a crypto twitter reader),
 "what_works": [str], "what_doesnt": [str], "red_flags": [str], "would_change_verdict": [str],
 "tweet": str (<=260 chars reply for X: verdict emoji, project name, 1-2 punchy evidence lines, link placeholder {url})}"""


QUICK_SYSTEM = """You are TechPad's quick auditor for a pre-launch tech token. You have ONE job, a hard budget of
{max_calls} tool calls and about {max_seconds} seconds: find the single most decisive thing that proves or disproves this
product, test it, and certify.

Pick the ONE test that settles it fastest, in this order of preference:
1. A live app URL: open it in the browser once and check it is a real, functioning product (not a template/"coming soon").
2. A GitHub repo: inspect it once — real original code with a plausible structure and recent commits, not a fork/README.
3. An API or docs URL: hit/fetch it once and see if it does what the description says.
4. Otherwise: nothing testable → say so.

Use at most {max_calls} tool calls. Do NOT explore. Do NOT run code or clone repos. One decisive observation, then reason.

Reasoning rules (be fair, not harsh): the score is the probability the product is real. A working app, a real repo, or a
demo that matches the description earns a high score even if other claims are unverified. Penalise only what you
observed missing, broken or contradicted. Give concrete fixes in would_change_verdict.

When done, output ONLY JSON:
{"project_name": str, "category": str, "one_liner": str, "detail_level": "none"|"vague"|"moderate"|"extensive",
 "claim": {"id": "C1", "claim": str, "how_to_test": str},
 "test": {"status": "WORKS"|"BROKEN"|"UNTESTABLE"|"FAKE"|"PARTIAL", "evidence": str, "steps": [str]},
 "verdict": "WORKS"|"PARTIAL"|"UNVERIFIED"|"LARP", "score": int, "confidence": int,
 "headline": str (<=100 chars), "summary": str (2-4 sentences), "what_works": [str], "what_doesnt": [str],
 "red_flags": [str], "would_change_verdict": [str]}"""


def _mock_llm(messages: list[dict], tools: list[dict] | None) -> dict:
    """Deterministic stand-in so the whole pipeline can be exercised without an API key or network."""
    import os
    last = messages[-1]["content"]
    text = last if isinstance(last, str) else json.dumps(last)
    time.sleep(float(os.environ.get("LARPCHECK_MOCK_DELAY", "0")))
    if tools:
        n_assist = sum(1 for m in messages if m["role"] == "assistant")
        url = os.environ.get("LARPCHECK_MOCK_URL", "https://example.com")
        script = [("browser_goto", {"url": url}), ("browser_act", {"action": "scroll", "value": "600"}),
                  ("fetch_url", {"url": url}), ("http_request", {"method": "GET", "url": url + "/api/status"}),
                  ("run_python", {"code": "print('probe ok')"})]
        if os.environ.get("LARPCHECK_MOCK_QUICK") == "1":
            script = script[:1]
        if n_assist < len(script):
            name, inp = script[n_assist]
            return {"stop_reason": "tool_use", "content": [{"type": "tool_use", "id": f"t{n_assist}", "name": name, "input": inp}]}
    if tools and "quick auditor" in (messages[0].get("content") if isinstance(messages[0].get("content"), str) else ""):
        pass
    if tools and os.environ.get("LARPCHECK_MOCK_QUICK") == "1":
        out = {"project_name": "MockProject", "category": "AI agent", "one_liner": "Mock quick audit", "detail_level": "moderate",
               "claim": {"id": "C1", "claim": "App loads and works", "how_to_test": "open it"},
               "test": {"status": "WORKS", "evidence": "mock: the page loaded", "steps": ["browser_goto"]},
               "verdict": "WORKS", "score": 78, "confidence": 70, "headline": "Mock: the app is real",
               "summary": "Mock quick audit passed.", "what_works": ["page loads"], "what_doesnt": [], "red_flags": [],
               "would_change_verdict": []}
        return {"stop_reason": "end_turn", "content": [{"type": "text", "text": json.dumps(out)}]}
    if tools:
        out = {"claim_results": [{"id": "C1", "status": "UNTESTABLE", "evidence": "mock run; no network", "steps": ["fetch_url example.com"]}],
               "observations": ["mock mode"], "larp_signals": [], "legit_signals": []}
        return {"stop_reason": "end_turn", "content": [{"type": "text", "text": json.dumps(out)}]}
    if "claims" in text and "verdict" in text.lower() and "claim_results" in text:
        out = {"verdict": "UNVERIFIED", "score": 35, "confidence": 40, "headline": "Mock verdict — nothing could be tested",
               "summary": "This is a mock run with no LLM and no network. The pipeline executed end to end.",
               "what_works": [], "what_doesnt": [], "red_flags": ["mock"], "would_change_verdict": ["a working demo link"],
               "tweet": "🟡 UNVERIFIED — mock run, nothing testable. Full report: {url}"}
        return {"stop_reason": "end_turn", "content": [{"type": "text", "text": json.dumps(out)}]}
    out = {"project_name": "MockProject", "category": "unknown", "one_liner": "Mock project", "detail_level": "vague",
           "product_links": {"app": None, "docs": None, "github": None, "api": None, "telegram": None},
           "claims": [{"id": "C1", "claim": "Has a product", "source": "mock", "testable": True,
                       "how_to_test": "open site", "importance": "core"}], "initial_red_flags": []}
    return {"stop_reason": "end_turn", "content": [{"type": "text", "text": json.dumps(out)}]}


def make_frame(name: str, inp: dict, out: str) -> dict | None:
    """Turn a tool call into something the live camera can draw (screenshots come from the browser itself)."""
    out = out or ""
    if name == "x_lookup":
        try:
            d = json.loads(out)
            if "author" in d or "screen_name" in d:
                return {"type": "tweet", "data": d}
        except Exception:
            pass
        return {"type": "http", "title": f"x_lookup {inp.get('url_or_handle', '')}", "body": out[:800]}
    if name in ("fetch_url", "http_request"):
        first = out.splitlines()[0] if out else ""
        return {"type": "http", "title": f"{inp.get('method', 'GET')} {inp.get('url', '')}", "status": first[:80],
                "body": out[:900]}
    if name == "solana_rpc":
        return {"type": "http", "title": f"solana {inp.get('method')}", "status": "RPC", "body": out[:900]}
    if name == "github_inspect":
        return {"type": "github", "title": inp.get("repo", ""), "body": out[:1200]}
    if name in ("run_python", "run_shell"):
        return {"type": "code", "title": name, "code": (inp.get("code") or inp.get("cmd") or "")[:700],
                "body": out[:900]}
    if name == "token_lookup":
        return {"type": "http", "title": "token metadata", "status": "pump.fun / dexscreener / rpc", "body": out[:900]}
    return None


def _as_understanding(u) -> dict:
    """Make whatever JSON the model returned safe to use."""
    if isinstance(u, list):
        u = {"claims": u}
    if not isinstance(u, dict):
        u = {}
    claims = u.get("claims")
    if not isinstance(claims, list):
        claims = []
    fixed = []
    for i, c in enumerate(claims):
        if isinstance(c, str):
            c = {"claim": c}
        if not isinstance(c, dict):
            continue
        c.setdefault("id", f"C{i + 1}")
        c.setdefault("claim", str(c.get("text") or c.get("description") or ""))
        c["testable"] = bool(c.get("testable", False))
        c.setdefault("how_to_test", "")
        c.setdefault("importance", "secondary")
        fixed.append(c)
    u["claims"] = fixed
    u.setdefault("project_name", None)
    u.setdefault("category", "unknown")
    u.setdefault("one_liner", "")
    u.setdefault("detail_level", "vague")
    if not isinstance(u.get("product_links"), dict):
        u["product_links"] = {}
    if not isinstance(u.get("initial_red_flags"), list):
        u["initial_red_flags"] = []
    return u


def _as_tests(t) -> dict:
    if isinstance(t, list):
        t = {"claim_results": t}
    if not isinstance(t, dict):
        t = {}
    for k in ("claim_results", "observations", "larp_signals", "legit_signals"):
        if not isinstance(t.get(k), list):
            t[k] = []
    return t


def _as_verdict(v) -> dict:
    if not isinstance(v, dict):
        v = {}
    vv = str(v.get("verdict", "UNVERIFIED")).upper()
    v["verdict"] = vv if vv in VERDICTS else "UNVERIFIED"
    try:
        v["score"] = max(0, min(100, int(v.get("score", 0) or 0)))
    except (TypeError, ValueError):
        v["score"] = 0
    v.setdefault("confidence", 0)
    v.setdefault("headline", "")
    v.setdefault("summary", "")
    for k in ("what_works", "what_doesnt", "red_flags", "would_change_verdict"):
        if not isinstance(v.get(k), list):
            v[k] = []
    v.setdefault("tweet", f"{v['verdict']} — {v['headline']} {{url}}")
    return v


class _Done(Exception):
    pass


def _quick_audit(llm: LLM, box: ToolBox, req, dossier: dict, result: dict, emit, t0: float) -> None:
    """One tool loop, one decisive test, one JSON. Hard caps on calls, seconds and dollars."""
    emit("phase", {"name": "test", "msg": "quick audit: one decisive test"})
    sys_prompt = (QUICK_SYSTEM.replace("{max_calls}", str(settings.QUICK_MAX_TOOL_CALLS))
                  .replace("{max_seconds}", str(settings.QUICK_MAX_SECONDS)))
    prompt = "MATERIAL:\n" + dossier_text(dossier, 14000) + "\n\nPick the one decisive test, run it, then output the JSON."
    deadline = t0 + settings.QUICK_MAX_SECONDS
    quick_tools = [t for t in TOOL_SCHEMAS if t["name"] in ("browser_goto", "fetch_url", "http_request", "github_inspect", "x_lookup")]

    def handler(name: str, inp: dict) -> str:
        if time.time() > deadline:
            return "ERROR: time is up. Output the final JSON now using what you already know."
        out = box.handle(name, inp)
        fr = make_frame(name, inp, out)
        if fr:
            emit("frame", fr)
        return out

    final, transcript = llm.tool_loop(sys_prompt, [{"role": "user", "content": prompt}], quick_tools, handler,
                                      settings.QUICK_MAX_TOOL_CALLS, on_event=emit)
    result["transcript"] = transcript
    try:
        j = parse_json(final)
    except ValueError:
        j = {}
    if not isinstance(j, dict):
        j = {}
    claim = j.get("claim") if isinstance(j.get("claim"), dict) else {"id": "C1", "claim": "(no claim identified)", "how_to_test": ""}
    claim.setdefault("id", "C1"); claim["testable"] = True; claim.setdefault("importance", "core")
    test = j.get("test") if isinstance(j.get("test"), dict) else {"status": "UNTESTABLE", "evidence": final[:500], "steps": []}
    test["id"] = claim["id"]
    result["understanding"] = _as_understanding({"project_name": j.get("project_name"), "category": j.get("category"),
                                                 "one_liner": j.get("one_liner"), "detail_level": j.get("detail_level"),
                                                 "claims": [claim], "initial_red_flags": []})
    for x in dossier.get("dead_links", []):
        result["understanding"]["initial_red_flags"].append(f"advertised link unreachable: {x['url']} ({x['why']})")
    result["dead_links"] = dossier.get("dead_links", [])
    result["tests"] = _as_tests({"claim_results": [test], "observations": [], "larp_signals": j.get("red_flags") or [],
                                 "legit_signals": j.get("what_works") or []})
    v = _as_verdict({k: j.get(k) for k in ("verdict", "score", "confidence", "headline", "summary", "what_works",
                                           "what_doesnt", "red_flags", "would_change_verdict")})
    if not v["headline"]:
        v["headline"] = "Quick audit could not reach a conclusion" if not j else v["headline"]
    v["tweet"] = f"{v['verdict']} {v['score']}/100 — {v['headline'][:150]} {{url}}"
    result["verdict"] = v
    emit("phase", {"name": "verdict", "msg": f"{v['verdict']} {v['score']}/100"})


def run_test(text: str, ca: str | None = None, x_url: str | None = None,
             on_event: Callable[[str, dict], None] | None = None, mode: str = "full") -> dict:
    """Run a LarpCheck test. mode="full" (deep) or "quick" (one decisive test, ~30s, for the launchpad).
    Returns a result dict (always; errors are captured inside)."""
    t0 = time.time()
    events: list[dict] = []
    quick = mode == "quick"

    def emit(kind: str, data: dict) -> None:
        ev = {"t": round(time.time() - t0, 1), "kind": kind, **data}
        if kind != "frame":  # frames (screenshots etc.) are streamed live only, never stored
            events.append(ev)
        if on_event:
            try:
                on_event(kind, ev)
            except Exception:
                pass

    llm = LLM(mock=_mock_llm if settings.MOCK_LLM else None,
              budget_usd=settings.QUICK_BUDGET_USD if quick else settings.BUDGET_USD_PER_TEST)
    req = parse_request(text, ca=ca, x_url=x_url)
    result: dict = {"input": text, "ca": req.ca, "x_url": req.x_url, "status": "running", "events": events, "mode": mode}
    box = ToolBox(on_frame=lambda b64, url, title, label="": emit("frame", {"type": "screenshot", "jpeg": b64, "url": url,
                                                                           "title": title, "label": label}))
    try:
        # ---- Phase 0: dossier (no LLM) --------------------------------------
        emit("phase", {"name": "research", "msg": "collecting token metadata, X, website, GitHub"})
        def research_frame(name: str, inp: dict, out: str) -> None:
            fr = make_frame(name, inp, out)
            if fr:
                emit("frame", fr)

        dossier = build_dossier(req, log=lambda m: emit("log", {"msg": m}), on_tool=research_frame)
        result["ca"], result["x_url"] = req.ca, req.x_url
        result["links"] = dossier.get("links")
        if settings.USE_BROWSER and not quick:
            emit("phase", {"name": "research", "msg": "opening the pages in the browser"})
            camera_tour(box.browser, req, dossier, log=lambda m: emit("log", {"msg": m}))

        if quick:
            _quick_audit(llm, box, req, dossier, result, emit, t0)
            result["status"] = "done"
            raise _Done()

        # ---- Phase 1: understand --------------------------------------------
        emit("phase", {"name": "understand", "msg": "extracting claims"})
        understanding = _as_understanding(llm.json_call(UNDERSTAND_SYSTEM, "MATERIAL:\n" + dossier_text(dossier) +
                                                        "\n\nProduce the JSON now."))
        # dead links found during research are red flags no matter what the model said
        for x in dossier.get("dead_links", []):
            flag = f"advertised link unreachable: {x['url']} ({x['why']})"
            if flag not in understanding["initial_red_flags"]:
                understanding["initial_red_flags"].append(flag)
        result["understanding"] = understanding
        result["dead_links"] = dossier.get("dead_links", [])
        claims = understanding.get("claims", [])
        emit("claims", {"count": len(claims), "project": understanding.get("project_name")})

        # ---- Phase 2: test --------------------------------------------------
        testable = [c for c in claims if c.get("testable")]
        if testable:
            emit("phase", {"name": "test", "msg": f"trying {len(testable)} testable claims"})
            sys_prompt = TEST_SYSTEM.replace("{max_calls}", str(settings.MAX_TOOL_CALLS))
            prompt = ("PROJECT UNDERSTANDING:\n" + json.dumps(understanding, indent=1) +
                      "\n\nBACKGROUND MATERIAL (already collected):\n" + dossier_text(dossier, 20000) +
                      "\n\nNow test the claims. Use tools. When finished, output the JSON.")
            deadline = t0 + settings.MAX_TEST_SECONDS

            def handler(name: str, inp: dict) -> str:
                if time.time() > deadline:
                    return "ERROR: time limit reached. Write the final JSON now."
                out = box.handle(name, inp)
                fr = make_frame(name, inp, out)
                if fr:
                    emit("frame", fr)
                return out

            final, transcript = llm.tool_loop(sys_prompt, [{"role": "user", "content": prompt}], TOOL_SCHEMAS,
                                              handler, settings.MAX_TOOL_CALLS, on_event=emit)
            result["transcript"] = transcript
            try:
                tests = _as_tests(parse_json(final))
            except ValueError:
                tests = {"claim_results": [], "observations": [final[:2000]], "larp_signals": [], "legit_signals": []}
        else:
            emit("phase", {"name": "test", "msg": "nothing testable — skipping hands-on phase"})
            tests = {"claim_results": [{"id": c["id"], "status": "UNTESTABLE", "evidence": c.get("how_to_test", ""),
                                        "steps": []} for c in claims],
                     "observations": ["No testable claims were found."], "larp_signals": [], "legit_signals": []}
            result["transcript"] = []
        result["tests"] = tests

        # ---- Phase 3: verdict ------------------------------------------------
        emit("phase", {"name": "verdict", "msg": "judging"})
        dead_note = ""
        if dossier.get("dead_links"):
            dead_note = "\n\nDEAD LINKS FOUND DURING RESEARCH:\n" + "\n".join(
                f"  - {x['url']}: {x['why']}" for x in dossier["dead_links"])
        verdict = _as_verdict(llm.json_call(VERDICT_SYSTEM, "CLAIMS:\n" + json.dumps(understanding, indent=1) +
                                            "\n\nTEST RESULTS:\n" + json.dumps(tests, indent=1) + dead_note +
                                            f"\n\nTool calls used: {len(result.get('transcript', []))}. Produce the verdict JSON.",
                                            force=True))  # the verdict always runs, even if the cap was just hit
        result["verdict"] = verdict
        result["status"] = "done"
    except _Done:
        pass
    except BudgetExceeded as e:
        result["status"] = "done"
        result.setdefault("verdict", {"verdict": "UNVERIFIED", "score": 0, "confidence": 0,
                                      "headline": "Budget exhausted before a verdict", "summary": str(e),
                                      "what_works": [], "what_doesnt": [], "red_flags": [], "would_change_verdict": [],
                                      "tweet": "🟡 UNVERIFIED — ran out of budget. {url}"})
    except Exception as e:
        result["status"] = "error"
        result["error"] = f"{type(e).__name__}: {e}"
        result["traceback"] = traceback.format_exc()[-3000:]
        emit("error", {"msg": result["error"]})
    finally:
        box.close()
    result["project_name"] = (result.get("understanding") or {}).get("project_name") or req.x_handle or req.ca or "unknown"
    result["cost_usd"] = round(llm.usage.cost_usd, 4)
    result["llm_calls"] = llm.usage.calls
    result["tool_calls"] = box.calls
    result["seconds"] = round(time.time() - t0, 1)
    result["model"] = llm.model
    return result
