"""The TechPad agent: research → test the main thing → (maybe) one follow-up test → verdict, in under a minute.

run_test(text) is synchronous and self-contained; the queue runs many of these in parallel threads.
"""
from __future__ import annotations

import json
import os
import threading
import time
import traceback
from typing import Callable

from .config import settings
from .llm import LLM, BudgetExceeded, DeadlineExceeded, parse_json
from .research import build_dossier, camera_tour, dossier_text, parse_request
from .tools import TOOL_SCHEMAS, ToolBox

VERDICTS = ("WORKS", "PARTIAL", "UNVERIFIED", "LARP")

TEST_SYSTEM = """You are TechPad. You check whether a Solana "tech" token's product actually works.
You get research material (token metadata, X, website, GitHub). The whole check has a hard one-minute limit, so you
run at most TWO short tests. Speed matters more than thoroughness.

A test = pick ONE main thing and run the single action that best shows whether it works: open the app and use its
core feature, hit its API, or inspect the repo's real code. At most {calls} tool calls per test. Do not browse around,
read marketing pages or re-fetch pages already in the material.

Rate what the test showed as "result":
- VERIFIED: you saw the core thing actually work.
- LEANS_WORKS: real signs it works, not fully proven (e.g. app loads and looks functional, but the main action needs
  a wallet or login).
- INCONCLUSIVE: 50/50, nothing decisive either way (nothing to try, needs access, ambiguous output).
- LEANS_BROKEN: strong signs it is not real (template or "coming soon" site, placeholder app, README-only or forked
  repo, claims contradicted by what you saw).
- BROKEN: you saw it fail or be fake.
Dead links the project advertises count against it. Never pay, sign transactions, connect a wallet or enter keys.

Verdict rules (apply exactly):
- After test 1: VERIFIED -> WORKS. LEANS_BROKEN or BROKEN -> LARP. LEANS_WORKS or INCONCLUSIVE -> you will be asked
  for test 2 on a DIFFERENT main thing.
- Test 1 LEANS_WORKS: test 2 VERIFIED or LEANS_WORKS -> WORKS; anything else -> PARTIAL.
- Test 1 INCONCLUSIVE: test 2 VERIFIED -> WORKS, LEANS_WORKS -> PARTIAL, INCONCLUSIVE -> UNVERIFIED (say plainly that
  both tests were inconclusive and why), LEANS_BROKEN or BROKEN -> LARP.

After each test output ONLY compact JSON with short strings, no code fences:
{"project_name": str, "category": str, "one_liner": str,
 "claim": str (the main thing you tested), "did": str (what you did), "evidence": str (what you observed),
 "result": "VERIFIED"|"LEANS_WORKS"|"INCONCLUSIVE"|"LEANS_BROKEN"|"BROKEN",
 "next_test": str (only if LEANS_WORKS or INCONCLUSIVE: the one different main thing to test next; else ""),
 "verdict": "WORKS"|"PARTIAL"|"UNVERIFIED"|"LARP", "score": int (0-100, probability it does what it says),
 "confidence": int (0-100), "headline": str (<=100 chars), "summary": str (2-3 sentences, covers every test so far),
 "what_works": [str], "what_doesnt": [str], "red_flags": [str], "would_change_verdict": [str]}"""

RESULTS = ("VERIFIED", "LEANS_WORKS", "INCONCLUSIVE", "LEANS_BROKEN", "BROKEN")
# how a test result shows up in the report's claim list
RESULT_STATUS = {"VERIFIED": "WORKS", "LEANS_WORKS": "PARTIAL", "INCONCLUSIVE": "UNTESTABLE",
                 "LEANS_BROKEN": "BROKEN", "BROKEN": "BROKEN"}
SCORE_RANGE = {"WORKS": (70, 100), "PARTIAL": (40, 74), "UNVERIFIED": (20, 55), "LARP": (0, 30)}
# tools the two tests may use: fast, single-shot actions only (no cloning or running code)
TEST_TOOLS = ("browser_goto", "browser_act", "fetch_url", "http_request", "github_inspect", "solana_rpc", "x_lookup")


def decide(r1: str | None, r2: str | None = None) -> str:
    """Verdict from the main test and, if one ran, the follow-up test."""
    if r1 == "VERIFIED":
        return "WORKS"
    if r1 in ("LEANS_BROKEN", "BROKEN"):
        return "LARP"
    if r2 is None:  # follow-up never ran (out of time)
        return "PARTIAL" if r1 == "LEANS_WORKS" else "UNVERIFIED"
    if r1 == "LEANS_WORKS":
        return "WORKS" if r2 in ("VERIFIED", "LEANS_WORKS") else "PARTIAL"
    # r1 inconclusive (50/50)
    return {"VERIFIED": "WORKS", "LEANS_WORKS": "PARTIAL", "INCONCLUSIVE": "UNVERIFIED"}.get(r2, "LARP")


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
    """Deterministic stand-in so the whole pipeline can be exercised without an API key or network.
    LARPCHECK_MOCK_R1 / LARPCHECK_MOCK_R2 pick the result of test 1 / test 2."""
    import os
    time.sleep(float(os.environ.get("LARPCHECK_MOCK_DELAY", "0")))
    url = os.environ.get("LARPCHECK_MOCK_URL", "https://example.com")
    if os.environ.get("LARPCHECK_MOCK_QUICK") == "1":
        if not any(m["role"] == "assistant" for m in messages):
            return {"stop_reason": "tool_use", "content": [{"type": "tool_use", "id": "t0", "name": "browser_goto", "input": {"url": url}}]}
        out = {"project_name": "MockProject", "category": "AI agent", "one_liner": "Mock quick audit", "detail_level": "moderate",
               "claim": {"id": "C1", "claim": "App loads and works", "how_to_test": "open it"},
               "test": {"status": "WORKS", "evidence": "mock: the page loaded", "steps": ["browser_goto"]},
               "verdict": "WORKS", "score": 78, "confidence": 70, "headline": "Mock: the app is real",
               "summary": "Mock quick audit passed.", "what_works": ["page loads"], "what_doesnt": [], "red_flags": [],
               "would_change_verdict": []}
        return {"stop_reason": "end_turn", "content": [{"type": "text", "text": json.dumps(out)}]}
    # full mode: each test = one tool call, then the JSON
    last_prompt = max(i for i, m in enumerate(messages) if m["role"] == "user" and isinstance(m["content"], str))
    second = last_prompt > 0
    if tools and not any(m["role"] == "assistant" for m in messages[last_prompt:]):
        name, inp = ("http_request", {"method": "GET", "url": url + "/api/status"}) if second else ("browser_goto", {"url": url})
        return {"stop_reason": "tool_use", "content": [{"type": "tool_use", "id": f"t{last_prompt}", "name": name, "input": inp}]}
    r = os.environ.get("LARPCHECK_MOCK_R2" if second else "LARPCHECK_MOCK_R1", "VERIFIED" if second else "LEANS_WORKS")
    out = {"project_name": "MockProject", "category": "AI agent", "one_liner": "Mock project",
           "claim": "the API answers" if second else "the app loads", "did": "mock request", "evidence": "mock output",
           "result": r, "next_test": "" if second else "call the API", "verdict": "WORKS", "score": 80,
           "confidence": 70, "headline": "Mock run", "summary": "This is a mock run with no LLM and no network.",
           "what_works": ["mock"], "what_doesnt": [], "red_flags": [], "would_change_verdict": []}
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


def _two_tests(llm: LLM, box: ToolBox, dossier: dict, result: dict, emit, deadline: float) -> None:
    """Test the main thing; if that leaves it open, test one more main thing; then decide. Fills result in place.
    Each piece of result is assigned whole, so a watchdog can read a consistent snapshot at any moment."""
    calls = settings.CALLS_PER_TEST
    sys_prompt = TEST_SYSTEM.replace("{calls}", str(calls))
    tools = [t for t in TOOL_SCHEMAS if t["name"] in TEST_TOOLS]
    transcript: list[dict] = []
    stop_tools_at = deadline - 30  # test 1 must leave room for test 2
    box.browser.deadline = deadline - 25

    def handler(name: str, inp: dict) -> str:
        if time.time() > stop_tools_at:
            return "ERROR: out of time for this test. Output the JSON now with what you already know."
        out = box.handle(name, inp)
        fr = make_frame(name, inp, out)
        if fr:
            emit("frame", fr)
        return out

    def run(messages: list[dict]) -> tuple[dict, str | None]:
        final, tr = llm.tool_loop(sys_prompt, messages, tools, handler, calls, on_event=emit, max_tokens=1500)
        transcript.extend(tr)
        result["transcript"] = list(transcript)
        try:
            j = parse_json(final)
        except ValueError:
            j = None
        if not isinstance(j, dict):
            return {}, None
        r = str(j.get("result", "")).upper()
        return j, (r if r in RESULTS else "INCONCLUSIVE")

    def publish(tests: list[tuple[dict, str]], j: dict) -> None:
        claims, claim_results = [], []
        for n, (t, r) in enumerate(tests, 1):
            claims.append({"id": f"C{n}", "claim": t.get("claim") or "(main claim)", "testable": True,
                           "importance": "core", "how_to_test": t.get("did", "")})
            claim_results.append({"id": f"C{n}", "status": RESULT_STATUS[r],
                                  "evidence": f"{r.replace('_', ' ').lower()}: {t.get('evidence', '')}",
                                  "steps": [t.get("did", "")]})
        u = _as_understanding({"project_name": j.get("project_name"), "category": j.get("category"),
                               "one_liner": j.get("one_liner"), "detail_level": "", "claims": claims,
                               "initial_red_flags": [f"advertised link unreachable: {x['url']} ({x['why']})"
                                                     for x in dossier.get("dead_links", [])]})
        v = _as_verdict({k: j.get(k) for k in ("score", "confidence", "headline", "summary", "what_works",
                                               "what_doesnt", "red_flags", "would_change_verdict")})
        v["verdict"] = decide(tests[0][1], tests[1][1] if len(tests) > 1 else None)
        lo, hi = SCORE_RANGE[v["verdict"]]
        v["score"] = max(lo, min(hi, v["score"]))
        if not v["headline"]:
            v["headline"] = f"{len(tests)} test{'s' if len(tests) > 1 else ''}: " + ", ".join(
                r.replace("_", " ").lower() for _, r in tests)
        v["tweet"] = f"{EMOJI[v['verdict']]} {v['verdict']} {v['score']}/100 — {v['headline'][:150]} {{url}}"
        result["understanding"] = u
        result["tests"] = _as_tests({"claim_results": claim_results, "observations": [],
                                     "larp_signals": v["red_flags"], "legit_signals": v["what_works"]})
        result["verdict"] = v

    emit("phase", {"name": "test1", "msg": "test 1: the main thing"})
    messages = [{"role": "user", "content": "MATERIAL:\n" + dossier_text(dossier, 14000) +
                 "\n\nRun TEST 1 now: pick the main thing and test it, then output the JSON."}]
    j1, r1 = run(messages)
    if r1 is None:
        raise DeadlineExceeded("the main test returned no usable result")
    emit("log", {"msg": f"test 1 → {r1.replace('_', ' ').lower()}: {j1.get('claim', '')}"})
    emit("claims", {"count": 1, "project": j1.get("project_name")})
    publish([(j1, r1)], j1)

    if r1 in ("LEANS_WORKS", "INCONCLUSIVE") and deadline - time.time() > 20:
        nxt = j1.get("next_test") or "a different core feature"
        emit("phase", {"name": "test2", "msg": f"test 2: {nxt}"[:160]})
        stop_tools_at = deadline - 15
        box.browser.deadline = deadline - 10
        messages.append({"role": "user", "content": f"Test 1 result: {r1}. Run TEST 2 now on one DIFFERENT main thing: "
                         f"{nxt}. At most {calls} tool calls, then output the same JSON: \"result\" is test 2's result; "
                         "verdict, score and summary cover both tests."})
        try:
            j2, r2 = run(messages)
        except BudgetExceeded:
            j2, r2 = {}, None
        if r2:
            emit("log", {"msg": f"test 2 → {r2.replace('_', ' ').lower()}: {j2.get('claim', '')}"})
            emit("claims", {"count": 2, "project": j2.get("project_name") or j1.get("project_name")})
            publish([(j1, r1), (j2, r2)], {**j1, **{k: v for k, v in j2.items() if v not in (None, "", [])}})
    elif r1 in ("LEANS_WORKS", "INCONCLUSIVE"):
        emit("log", {"msg": "no time left for a second test"})


EMOJI = {"WORKS": "🟢", "PARTIAL": "🟠", "UNVERIFIED": "🟡", "LARP": "🔴"}


def run_test(text: str, ca: str | None = None, x_url: str | None = None,
             on_event: Callable[[str, dict], None] | None = None, mode: str = "full") -> dict:
    """Run a TechPad test. mode="full" (main test + at most one follow-up) or "quick" (one decisive test, for the
    launchpad). Never takes longer than settings.TIME_LIMIT_SECONDS. Returns a result dict (errors captured inside)."""
    t0 = time.time()
    deadline = t0 + settings.TIME_LIMIT_SECONDS
    events: list[dict] = []
    quick = mode == "quick"
    abandoned = threading.Event()  # set when the watchdog gives up on the worker thread

    def emit(kind: str, data: dict) -> None:
        if abandoned.is_set():
            return
        ev = {"t": round(time.time() - t0, 1), "kind": kind, **data}
        if kind != "frame":  # frames (screenshots etc.) are streamed live only, never stored
            events.append(ev)
        if on_event:
            try:
                on_event(kind, ev)
            except Exception:
                pass

    llm = LLM(mock=_mock_llm if settings.MOCK_LLM else None,
              budget_usd=settings.QUICK_BUDGET_USD if quick else settings.BUDGET_USD_PER_TEST,
              deadline=deadline - 1)
    req = parse_request(text, ca=ca, x_url=x_url)
    result: dict = {"input": text, "ca": req.ca, "x_url": req.x_url, "status": "running", "events": events, "mode": mode}
    box = ToolBox(on_frame=lambda b64, url, title, label="": emit("frame", {"type": "screenshot", "jpeg": b64, "url": url,
                                                                           "title": title, "label": label}))

    def work() -> None:
        try:
            # ---- research (no LLM), time-boxed --------------------------------
            research_until = t0 + settings.RESEARCH_SECONDS
            emit("phase", {"name": "research", "msg": "collecting token metadata, X, website, GitHub"})

            def research_frame(name: str, inp: dict, out: str) -> None:
                fr = make_frame(name, inp, out)
                if fr:
                    emit("frame", fr)

            dossier = build_dossier(req, log=lambda m: emit("log", {"msg": m}), on_tool=research_frame,
                                    deadline=research_until)
            result["ca"], result["x_url"] = req.ca, req.x_url
            result["links"] = dossier.get("links")
            result["dead_links"] = dossier.get("dead_links", [])
            if settings.USE_BROWSER and not quick and research_until - time.time() > 3:
                emit("phase", {"name": "research", "msg": "opening the pages in the browser"})
                box.browser.deadline = research_until
                camera_tour(box.browser, req, dossier, log=lambda m: emit("log", {"msg": m}), deadline=research_until)
            box.browser.deadline = deadline - 10  # always leave time to write the verdict

            if quick:
                _quick_audit(llm, box, req, dossier, result, emit, t0)
            else:
                _two_tests(llm, box, dossier, result, emit, deadline)
                v = result["verdict"]
                emit("phase", {"name": "verdict", "msg": f"{v['verdict']} {v['score']}/100"})
            result["status"] = "done"
        except BudgetExceeded as e:
            result["status"] = "done"
            if "verdict" not in result:
                result["verdict"] = _out_of_time_verdict(str(e))
        except Exception as e:
            result["status"] = "error"
            result["error"] = f"{type(e).__name__}: {e}"
            result["traceback"] = traceback.format_exc()[-3000:]
            emit("error", {"msg": result["error"]})
        finally:
            box.close()

    worker = threading.Thread(target=work, name="techpad-test", daemon=True)
    worker.start()
    worker.join(max(0.0, deadline - time.time()))
    if worker.is_alive():  # hard cap: report what we have; the thread winds down on its own (LLM/browser deadlines)
        abandoned.set()
        result = dict(result)
        result["status"] = "done"
        if "verdict" not in result:
            result["verdict"] = _out_of_time_verdict("hit the time limit before a verdict")
        result["events"] = list(events)
    result["project_name"] = (result.get("understanding") or {}).get("project_name") or req.x_handle or req.ca or "unknown"
    result["cost_usd"] = round(llm.usage.cost_usd, 4)
    result["llm_calls"] = llm.usage.calls
    result["tool_calls"] = box.calls
    result["seconds"] = round(time.time() - t0, 1)
    result["model"] = llm.model
    return result


def _out_of_time_verdict(why: str) -> dict:
    return {"verdict": "UNVERIFIED", "score": 0, "confidence": 0, "headline": "Ran out of time before a verdict",
            "summary": f"The test stopped early ({why}), so nothing was verified.", "what_works": [],
            "what_doesnt": [], "red_flags": [], "would_change_verdict": ["re-run the test"],
            "tweet": "🟡 UNVERIFIED — ran out of time. {url}"}
