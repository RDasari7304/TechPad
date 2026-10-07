"""HTTP API + frontend. Starlette app; run with `python -m larpcheck serve`."""
from __future__ import annotations

import asyncio
import html
import json
import os
import queue as _queue
from pathlib import Path

from starlette.applications import Starlette
from starlette.middleware.cors import CORSMiddleware
from starlette.requests import Request
from starlette.responses import FileResponse, HTMLResponse, JSONResponse, Response, StreamingResponse
from starlette.routing import Mount, Route
from starlette.staticfiles import StaticFiles

from .config import ROOT, settings
from .db import DB
from .launchpad import LaunchError, Launchpad, badge_svg
from .queue import JobManager

FRONTEND = ROOT / "frontend"
db = DB()
jobs = JobManager(db)
pad = Launchpad(db, jobs)
os.makedirs(settings.UPLOAD_DIR, exist_ok=True)


async def index(_: Request):
    return FileResponse(FRONTEND / "index.html")


async def create_test(request: Request):
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "invalid JSON"}, status_code=400)
    text = (body.get("text") or body.get("query") or "").strip()
    if not text:
        return JSONResponse({"error": "text is required (CA, X link, or a sentence with both)"}, status_code=400)
    if len(text) > 2000:
        return JSONResponse({"error": "too long"}, status_code=400)
    res = jobs.submit(text, source="web", requester=request.client.host if request.client else "",
                      force=bool(body.get("force")), ca=body.get("ca"), x_url=body.get("x_url"))
    return JSONResponse(res, status_code=202 if res["status"] != "done" else 200)


async def get_test(request: Request):
    row = db.get(request.path_params["tid"], full=True)
    if not row:
        return JSONResponse({"error": "not found"}, status_code=404)
    return JSONResponse(row)


async def stream_test(request: Request):
    """Server-sent events for live progress."""
    tid = request.path_params["tid"]
    row = db.get(tid, full=False)
    if not row:
        return JSONResponse({"error": "not found"}, status_code=404)
    q: "_queue.Queue[dict]" = _queue.Queue()
    backlog = jobs.subscribe(tid, q.put)

    async def gen():
        try:
            for ev in backlog:
                yield f"data: {json.dumps(ev)}\n\n"
            if row["status"] in ("done", "error"):
                yield f"data: {json.dumps({'kind': 'done', 'status': row['status']})}\n\n"
                return
            while True:
                try:
                    ev = q.get_nowait()
                except _queue.Empty:
                    await asyncio.sleep(0.5)
                    if await request.is_disconnected():
                        return
                    yield ": ping\n\n"
                    continue
                yield f"data: {json.dumps(ev)}\n\n"
                if ev.get("kind") == "done":
                    return
        finally:
            jobs.unsubscribe(tid, q.put)

    return StreamingResponse(gen(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


async def lookup(request: Request):
    """Has this CA / handle already been tested?"""
    key = request.query_params.get("key", "").strip()
    if not key:
        return JSONResponse({"error": "key required"}, status_code=400)
    row = db.latest_for_key(key, done_only=False)
    return JSONResponse({"found": bool(row), "test": row})


async def archive(request: Request):
    p = request.query_params
    rows = db.archive(q=p.get("q", ""), verdict=p.get("verdict", ""),
                      limit=min(int(p.get("limit", 50)), 200), offset=int(p.get("offset", 0)))
    return JSONResponse({"tests": rows, "stats": db.stats()})


async def live(_: Request):
    return JSONResponse({"rooms": jobs.live()})


async def status(_: Request):
    return JSONResponse({"ok": True, **jobs.status(), "model": settings.MODEL, "mock": settings.MOCK_LLM,
                         "budget_per_test_usd": settings.BUDGET_USD_PER_TEST})


# ------------------------------------------------------------------ launchpad
def _err(e: Exception, code: int = 400):
    return JSONResponse({"error": str(e)}, status_code=code)


async def launch_create(request: Request):
    try:
        body = await request.json()
    except Exception:
        return _err(ValueError("invalid JSON"))
    try:
        return JSONResponse(await asyncio.to_thread(pad.submit, body), status_code=202)
    except LaunchError as e:
        return _err(e)


async def launch_get(request: Request):
    try:
        return JSONResponse(pad.get(request.path_params["lid"]))
    except LaunchError as e:
        return _err(e, 404)


async def launch_update(request: Request):
    try:
        body = await request.json()
        return JSONResponse(await asyncio.to_thread(pad.update, request.path_params["lid"], body), status_code=202)
    except LaunchError as e:
        return _err(e)


async def launch_reaudit(request: Request):
    try:
        return JSONResponse(pad.start_audit(request.path_params["lid"]), status_code=202)
    except LaunchError as e:
        return _err(e)


async def launch_prepare(request: Request):
    body = await request.json()
    try:
        out = await asyncio.to_thread(pad.prepare, request.path_params["lid"], body.get("wallet", ""), body.get("mint", ""),
                                      float(body.get("dev_buy_sol") or 0), int(body.get("slippage") or 10),
                                      float(body.get("priority_fee") or 0.0005))
        return JSONResponse(out)
    except LaunchError as e:
        return _err(e)
    except Exception as e:  # network etc.
        return _err(RuntimeError(f"prepare failed: {e}"), 502)


async def launch_confirm(request: Request):
    body = await request.json()
    try:
        return JSONResponse(await asyncio.to_thread(pad.confirm, request.path_params["lid"], body.get("signature", ""),
                                                    body.get("mint", "")))
    except LaunchError as e:
        return _err(e)


async def launches(request: Request):
    rows = [pad._decorate(L) for L in db.launches(status=request.query_params.get("status") or "launched", limit=60)]
    return JSONResponse({"launches": rows, "min_score": settings.LAUNCH_MIN_SCORE,
                         "verdicts": list(settings.LAUNCH_ALLOWED_VERDICTS)})


async def coin_json(request: Request):
    L = pad.coin(request.path_params["mint"])
    return JSONResponse(L) if L else _err(ValueError("no coin launched here with that mint"), 404)


async def coin_page(request: Request):
    """Server-rendered shell with OG tags; the page fills itself from /api/coin/<mint>."""
    mint = request.path_params["mint"]
    L = pad.coin(mint)
    tpl = (FRONTEND / "coin.html").read_text(encoding="utf-8")
    a = (L or {}).get("audit") or {}
    title = f"{L['name']} (${L['ticker']}) — TechPad audited" if L else "Unknown coin — TechPad"
    desc = (a.get("headline") or (L or {}).get("description") or "Audited tech launch on pump.fun")[:200]
    img = f"{settings.PUBLIC_URL}{L['image_path']}" if L and L.get("image_path") else ""
    page = (tpl.replace("{{TITLE}}", html.escape(title)).replace("{{DESC}}", html.escape(desc))
            .replace("{{IMAGE}}", html.escape(img)).replace("{{MINT}}", html.escape(mint)))
    return HTMLResponse(page, status_code=200 if L else 404)


async def badge(request: Request):
    L = pad.coin(request.path_params["mint"])
    return Response(badge_svg(L), media_type="image/svg+xml", headers={"Cache-Control": "public, max-age=300"})


async def launch_config(_: Request):
    return JSONResponse({"min_score": settings.LAUNCH_MIN_SCORE, "verdicts": list(settings.LAUNCH_ALLOWED_VERDICTS),
                         "rpc": settings.SOLANA_RPC, "public_url": settings.PUBLIC_URL,
                         "max_image_mb": settings.MAX_IMAGE_MB, "max_video_mb": settings.MAX_VIDEO_MB})


routes = [
    Route("/api/launch", launch_create, methods=["POST"]),
    Route("/api/launch/config", launch_config),
    Route("/api/launch/{lid}", launch_get),
    Route("/api/launch/{lid}/reaudit", launch_reaudit, methods=["POST"]),
    Route("/api/launch/{lid}/update", launch_update, methods=["POST"]),
    Route("/api/launch/{lid}/prepare", launch_prepare, methods=["POST"]),
    Route("/api/launch/{lid}/confirm", launch_confirm, methods=["POST"]),
    Route("/api/launches", launches),
    Route("/api/coin/{mint}", coin_json),
    Route("/coin/{mint}", coin_page),
    Route("/badge/{mint}.svg", badge),
    Mount("/uploads", StaticFiles(directory=settings.UPLOAD_DIR), name="uploads"),
    Route("/", index),
    Route("/api/tests", create_test, methods=["POST"]),
    Route("/api/tests/{tid}", get_test),
    Route("/api/tests/{tid}/stream", stream_test),
    Route("/api/lookup", lookup),
    Route("/api/archive", archive),
    Route("/api/status", status),
    Route("/api/live", live),
    Mount("/static", StaticFiles(directory=str(FRONTEND)), name="static"),
]

app = Starlette(routes=routes)
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])


def serve() -> None:
    import uvicorn
    print(f"TechPad on http://localhost:{settings.PORT}  (workers={settings.WORKERS}, model={settings.MODEL}, mock={settings.MOCK_LLM})")
    uvicorn.run(app, host=settings.HOST, port=settings.PORT, log_level="info")
