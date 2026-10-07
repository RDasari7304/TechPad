"""Job manager: unbounded queue, N parallel agent workers, dedupe + cache, live progress streams."""
from __future__ import annotations

import queue
import threading
import time
from collections import defaultdict
from typing import Callable

from .agent import run_test
from .config import settings
from .db import DB
from .research import parse_request


class JobManager:
    def __init__(self, db: DB, workers: int = settings.WORKERS) -> None:
        self.db = db
        self.q: "queue.Queue[str]" = queue.Queue()
        self.inflight: dict[str, str] = {}             # key -> test id currently running/queued
        self.events: dict[str, list[dict]] = defaultdict(list)
        self.listeners: dict[str, list[Callable[[dict], None]]] = defaultdict(list)
        self.done_callbacks: dict[str, list[Callable[[dict], None]]] = defaultdict(list)
        self.sources: dict[str, str] = {}
        self._lock = threading.Lock()
        self.threads = [threading.Thread(target=self._worker, name=f"larp-{i}", daemon=True) for i in range(workers)]
        for t in self.threads:
            t.start()

    # ---- public ------------------------------------------------------------
    def submit(self, text: str, source: str = "web", requester: str = "", force: bool = False,
               ca: str | None = None, x_url: str | None = None,
               on_done: Callable[[dict], None] | None = None, key: str | None = None) -> dict:
        """Returns {"id", "status", "cached": bool}. Same CA from many devices -> one run."""
        req = parse_request(text, ca=ca, x_url=x_url)
        key = key or req.key()
        with self._lock:
            if not force:
                tid = self.inflight.get(key)
                if tid:  # already queued/running: attach
                    if on_done:
                        self.done_callbacks[tid].append(on_done)
                    return {"id": tid, "status": "attached", "cached": False}
                cached = self.db.latest_for_key(key, max_age_hours=settings.CACHE_HOURS)
                if cached:
                    if on_done:
                        on_done(cached)
                    return {"id": cached["id"], "status": "done", "cached": True}
            tid = self.db.create(key, text, source, requester, ca=req.ca, x_url=req.x_url)
            self.inflight[key] = tid
            self.sources[tid] = source
            if on_done:
                self.done_callbacks[tid].append(on_done)
        self.events[tid].append({"t": 0, "kind": "queued", "msg": f"queued (position ~{self.q.qsize()})"})
        self.q.put(tid)
        return {"id": tid, "status": "queued", "cached": False}

    def subscribe(self, tid: str, cb: Callable[[dict], None]) -> list[dict]:
        """Register a live listener; returns the backlog of events so far."""
        with self._lock:
            self.listeners[tid].append(cb)
            return list(self.events[tid])

    def unsubscribe(self, tid: str, cb: Callable[[dict], None]) -> None:
        with self._lock:
            if cb in self.listeners[tid]:
                self.listeners[tid].remove(cb)

    def live(self) -> list[dict]:
        """Rooms: every queued/running test with its current phase, viewers and last camera frame."""
        with self._lock:
            items = list(self.inflight.items())
            rooms = []
            for key, tid in items:
                if self.sources.get(tid) == "launchpad":  # private pre-launch audits are not public rooms
                    continue
                evs = self.events.get(tid, [])
                phase = next((e for e in reversed(evs) if e.get("kind") == "phase"), None)
                claims = next((e for e in reversed(evs) if e.get("kind") == "claims"), None)
                frame = next((e for e in reversed(evs) if e.get("kind") == "frame"), None)
                started = any(e.get("kind") == "started" for e in evs)
                rooms.append({
                    "id": tid, "key": key, "status": "running" if started else "queued",
                    "project": (claims or {}).get("project"), "phase": (phase or {}).get("name"),
                    "phase_msg": (phase or {}).get("msg"), "elapsed": (evs[-1].get("t") if evs else 0),
                    "viewers": len(self.listeners.get(tid, [])), "events": len(evs),
                    "last_frame": ({"type": frame.get("type"), "url": frame.get("url"), "title": frame.get("title"),
                                    "jpeg": frame.get("jpeg")} if frame else None),
                })
        return rooms

    def status(self) -> dict:
        return {"queued": self.q.qsize(), "workers": len(self.threads),
                "inflight": len(self.inflight), **self.db.stats()}

    # ---- internals -----------------------------------------------------------
    MAX_FRAMES_KEPT = 25  # screenshots kept in the backlog for late joiners

    def _emit(self, tid: str, ev: dict) -> None:
        with self._lock:
            evs = self.events[tid]
            evs.append(ev)
            if ev.get("kind") == "frame":
                frames = [i for i, e in enumerate(evs) if e.get("kind") == "frame"]
                if len(frames) > self.MAX_FRAMES_KEPT:
                    del evs[frames[0]]
            cbs = list(self.listeners[tid])
        for cb in cbs:
            try:
                cb(ev)
            except Exception:
                pass

    def _worker(self) -> None:
        while True:
            tid = self.q.get()
            row = self.db.get(tid, full=False)
            if not row:
                continue
            self.db.mark_running(tid)
            self._emit(tid, {"t": 0, "kind": "started", "msg": "worker picked up the job"})
            mode = "quick" if row.get("source") == "launchpad" else "full"
            try:
                result = run_test(row["input"], ca=row.get("ca"), x_url=row.get("x_url"),
                                  on_event=lambda k, ev: self._emit(tid, ev), mode=mode)
            except Exception as e:  # should not happen (run_test catches), belt and braces
                result = {"status": "error", "error": str(e), "input": row["input"]}
            self.db.finish(tid, result)
            final = self.db.get(tid, full=True)
            self._emit(tid, {"t": result.get("seconds", 0), "kind": "done", "status": result.get("status"),
                             "verdict": (result.get("verdict") or {}).get("verdict")})
            with self._lock:
                self.inflight = {k: v for k, v in self.inflight.items() if v != tid}
                cbs = self.done_callbacks.pop(tid, [])
                # keep events around for a while, then drop to bound memory
                threading.Timer(600, lambda: self.events.pop(tid, None)).start()
            for cb in cbs:
                try:
                    cb(final)
                except Exception:
                    pass
            self.q.task_done()
