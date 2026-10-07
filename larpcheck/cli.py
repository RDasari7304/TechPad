"""Command line.

  python -m larpcheck test "<CA or X link or sentence>" [--x URL] [--ca CA] [--force] [--json]
  python -m larpcheck archive [query]
  python -m larpcheck show <test id>
  python -m larpcheck serve            # web UI + API
  python -m larpcheck xbot             # X mention bot (needs keys)
  python -m larpcheck all              # serve + xbot in one process
"""
from __future__ import annotations

import argparse
import json
import sys
import threading
import time

from .config import settings

EMOJI = {"WORKS": "🟢", "PARTIAL": "🟠", "UNVERIFIED": "🟡", "LARP": "🔴"}


def _print_result(res: dict) -> None:
    v = res.get("verdict") or {}
    u = res.get("understanding") or {}
    t = res.get("tests") or {}
    print("\n" + "=" * 72)
    print(f"{EMOJI.get(v.get('verdict'), '⚪')} {v.get('verdict', res.get('status', '?'))}  score {v.get('score', '-')}/100  "
          f"confidence {v.get('confidence', '-')}  —  {res.get('project_name')}")
    print("=" * 72)
    if res.get("error"):
        print("ERROR:", res["error"])
    print(f"\n{v.get('headline', '')}\n\n{v.get('summary', '')}\n")
    if u.get("one_liner"):
        print(f"What it claims to be: {u['one_liner']}  [{u.get('category')}, detail: {u.get('detail_level')}]")
    status_of = {c.get("id"): c for c in t.get("claim_results", [])}
    if u.get("claims"):
        print("\nClaims:")
        for c in u["claims"]:
            r = status_of.get(c["id"], {})
            print(f"  [{r.get('status', 'UNTESTED'):10}] {c['claim']}")
            if r.get("evidence"):
                print(f"               ↳ {r['evidence'][:300]}")
    for label, key in (("Works", "what_works"), ("Doesn't", "what_doesnt"), ("Red flags", "red_flags"),
                       ("Would change verdict", "would_change_verdict")):
        if v.get(key):
            print(f"\n{label}:")
            for x in v[key]:
                print(f"  • {x}")
    if v.get("tweet"):
        print(f"\nTweet reply:\n  {v['tweet']}")
    print(f"\n[{res.get('seconds')}s, {res.get('llm_calls')} LLM calls, {res.get('tool_calls')} tool calls, "
          f"~${res.get('cost_usd')} with {res.get('model')}]")


def cmd_test(a: argparse.Namespace) -> int:
    from .db import DB
    from .queue import JobManager
    db = DB()
    jobs = JobManager(db, workers=1)
    text = " ".join(a.text)
    done = threading.Event()
    holder: dict = {}

    def on_done(row: dict) -> None:
        holder["row"] = row
        done.set()

    sub = jobs.submit(text, source="cli", requester="cli", force=a.force, ca=a.ca, x_url=a.x, on_done=on_done)
    if sub.get("cached"):
        print(f"(cached result from the archive — use --force to re-test)")
    else:
        print(f"test {sub['id']} queued; working...")
        seen = 0
        finished = False
        while not finished:
            finished = done.wait(0.5)
            evs = jobs.events.get(sub["id"], [])
            for ev in evs[seen:]:
                k = ev.get("kind")
                if k == "phase":
                    print(f"  ▶ {ev['name']}: {ev.get('msg', '')}")
                elif k == "log":
                    print(f"    - {ev['msg']}")
                elif k == "tool":
                    print(f"    🔧 {ev['name']} {json.dumps(ev.get('input'))[:110]}")
                elif k == "claims":
                    print(f"    found {ev['count']} claims for {ev.get('project')}")
                elif k == "error":
                    print(f"    !! {ev['msg']}")
            seen = len(evs)
    row = holder.get("row") or db.get(sub["id"], full=True)
    res = row.get("result") or {}
    if a.json:
        print(json.dumps(res, indent=1))
    else:
        _print_result(res)
        print(f"id: {row['id']}  (python -m larpcheck show {row['id']} --json for everything)")
    return 0 if res.get("status") == "done" else 1


def cmd_archive(a: argparse.Namespace) -> int:
    from .db import DB
    db = DB()
    rows = db.archive(q=" ".join(a.query), verdict=a.verdict or "", limit=a.limit)
    st = db.stats()
    print(f"{st['total']} tests, ${st['spent_usd']} spent, by verdict: {st['by_verdict']}\n")
    for r in rows:
        ts = time.strftime("%Y-%m-%d %H:%M", time.localtime(r["created_at"]))
        print(f"{ts}  {EMOJI.get(r['verdict'], '⚪')} {str(r['verdict'] or r['status']):10} {str(r['score'] or '-'):>3}  "
              f"{(r['project_name'] or '')[:24]:24}  {r['ca'] or r['x_url'] or ''}  [{r['id']}]")
    return 0


def cmd_show(a: argparse.Namespace) -> int:
    from .db import DB
    row = DB().get(a.id, full=True)
    if not row:
        print("not found")
        return 1
    if a.json:
        print(json.dumps(row, indent=1))
    else:
        _print_result(row.get("result") or {})
    return 0


def cmd_serve(_: argparse.Namespace) -> int:
    from .server import serve
    serve()
    return 0


def cmd_xbot(_: argparse.Namespace) -> int:
    from .db import DB
    from .queue import JobManager
    from .xbot import XBot
    db = DB()
    XBot(db, JobManager(db)).run_forever()
    return 0


def cmd_all(_: argparse.Namespace) -> int:
    from . import server
    from .xbot import XBot
    if settings.X_BEARER_TOKEN:
        bot = XBot(server.db, server.jobs)
        threading.Thread(target=bot.run_forever, daemon=True, name="xbot").start()
    else:
        print("[xbot] no X keys in .env — running web only")
    server.serve()
    return 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="larpcheck", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sp = p.add_subparsers(dest="cmd", required=True)
    t = sp.add_parser("test", help="test a project now");
    t.add_argument("text", nargs="+", help="CA, X link, or 'check this: <CA> <link>'")
    t.add_argument("--x", help="project X/Twitter URL (tweet or profile)")
    t.add_argument("--ca", help="token contract address")
    t.add_argument("--force", action="store_true", help="ignore cached result")
    t.add_argument("--json", action="store_true")
    t.set_defaults(fn=cmd_test)
    ar = sp.add_parser("archive", help="list past tests"); ar.add_argument("query", nargs="*")
    ar.add_argument("--verdict"); ar.add_argument("--limit", type=int, default=50); ar.set_defaults(fn=cmd_archive)
    sh = sp.add_parser("show"); sh.add_argument("id"); sh.add_argument("--json", action="store_true"); sh.set_defaults(fn=cmd_show)
    sp.add_parser("serve", help="web UI + API").set_defaults(fn=cmd_serve)
    sp.add_parser("xbot", help="X mention bot").set_defaults(fn=cmd_xbot)
    sp.add_parser("all", help="web + xbot").set_defaults(fn=cmd_all)
    a = p.parse_args(argv)
    return a.fn(a)


if __name__ == "__main__":
    sys.exit(main())
