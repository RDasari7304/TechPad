"""SQLite store for every test ever run (the public archive)."""
from __future__ import annotations

import json
import os
import sqlite3
import threading
import time
import uuid

from .config import settings

SCHEMA = """
CREATE TABLE IF NOT EXISTS tests (
  id TEXT PRIMARY KEY,
  key TEXT NOT NULL,                -- dedupe key (CA or @handle)
  ca TEXT, x_url TEXT, project_name TEXT,
  input TEXT, source TEXT,          -- source: cli | web | x
  requester TEXT,
  status TEXT NOT NULL,             -- queued | running | done | error
  verdict TEXT, score INTEGER, headline TEXT,
  result_json TEXT,
  cost_usd REAL DEFAULT 0,
  created_at REAL NOT NULL, started_at REAL, finished_at REAL
);
CREATE INDEX IF NOT EXISTS idx_tests_key ON tests(key);
CREATE INDEX IF NOT EXISTS idx_tests_created ON tests(created_at DESC);
CREATE TABLE IF NOT EXISTS x_state (k TEXT PRIMARY KEY, v TEXT);
CREATE TABLE IF NOT EXISTS launches (
  id TEXT PRIMARY KEY,
  name TEXT, ticker TEXT, description TEXT,
  image_path TEXT, video_url TEXT, video_path TEXT,
  github TEXT, website TEXT, app_url TEXT, docs_url TEXT, x_url TEXT, telegram TEXT,
  onboarding TEXT, creator_wallet TEXT,
  status TEXT NOT NULL,              -- auditing | audited | launched | failed
  audit_test_id TEXT, mint TEXT, signature TEXT, metadata_uri TEXT,
  created_at REAL NOT NULL, launched_at REAL
);
CREATE INDEX IF NOT EXISTS idx_launch_mint ON launches(mint);
"""


class DB:
    def __init__(self, path: str = settings.DB_PATH) -> None:
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        self.path = path
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.executescript(SCHEMA)
        cols = {r["name"] for r in self._conn.execute("PRAGMA table_info(launches)")}
        if "website" not in cols:  # databases created before the website field existed
            self._conn.execute("ALTER TABLE launches ADD COLUMN website TEXT")
            self._conn.commit()

    def _run(self, sql: str, args: tuple = ()) -> sqlite3.Cursor:
        with self._lock:
            cur = self._conn.execute(sql, args)
            self._conn.commit()
            return cur

    # --- writes ---------------------------------------------------------------
    def create(self, key: str, text: str, source: str, requester: str = "", ca: str | None = None,
               x_url: str | None = None) -> str:
        tid = uuid.uuid4().hex[:12]
        self._run("INSERT INTO tests (id,key,ca,x_url,input,source,requester,status,created_at) VALUES (?,?,?,?,?,?,?,?,?)",
                  (tid, key, ca, x_url, text, source, requester, "queued", time.time()))
        return tid

    def mark_running(self, tid: str) -> None:
        self._run("UPDATE tests SET status='running', started_at=? WHERE id=?", (time.time(), tid))

    def finish(self, tid: str, result: dict) -> None:
        v = result.get("verdict") or {}
        self._run("""UPDATE tests SET status=?, verdict=?, score=?, headline=?, project_name=?, ca=?, x_url=?,
                     result_json=?, cost_usd=?, finished_at=? WHERE id=?""",
                  (result.get("status", "done"), v.get("verdict"), v.get("score"), v.get("headline"),
                   result.get("project_name"), result.get("ca"), result.get("x_url"),
                   json.dumps(result), result.get("cost_usd", 0), time.time(), tid))

    # --- reads ----------------------------------------------------------------
    @staticmethod
    def _row(r: sqlite3.Row | None, full: bool = False) -> dict | None:
        if r is None:
            return None
        d = dict(r)
        if full and d.get("result_json"):
            d["result"] = json.loads(d["result_json"])
        d.pop("result_json", None)
        return d

    def get(self, tid: str, full: bool = True) -> dict | None:
        return self._row(self._run("SELECT * FROM tests WHERE id=?", (tid,)).fetchone(), full)

    def latest_for_key(self, key: str, max_age_hours: float | None = None, done_only: bool = True) -> dict | None:
        sql = "SELECT * FROM tests WHERE key=?"
        args: list = [key]
        if done_only:
            sql += " AND status IN ('done')"
        else:
            sql += " AND status IN ('queued','running','done')"
        if max_age_hours:
            sql += " AND created_at > ?"
            args.append(time.time() - max_age_hours * 3600)
        sql += " ORDER BY created_at DESC LIMIT 1"
        return self._row(self._run(sql, tuple(args)).fetchone(), full=True)

    def archive(self, q: str = "", verdict: str = "", limit: int = 50, offset: int = 0) -> list[dict]:
        sql = "SELECT id,key,ca,x_url,project_name,source,status,verdict,score,headline,cost_usd,created_at,finished_at FROM tests WHERE 1=1"
        args: list = []
        if q:
            sql += " AND (ca LIKE ? OR project_name LIKE ? OR x_url LIKE ? OR input LIKE ?)"
            args += [f"%{q}%"] * 4
        if verdict:
            sql += " AND verdict=?"
            args.append(verdict.upper())
        sql += " ORDER BY created_at DESC LIMIT ? OFFSET ?"
        args += [limit, offset]
        return [dict(r) for r in self._run(sql, tuple(args)).fetchall()]

    def stats(self) -> dict:
        rows = self._run("SELECT verdict, COUNT(*) c FROM tests WHERE status='done' GROUP BY verdict").fetchall()
        total = self._run("SELECT COUNT(*) c, COALESCE(SUM(cost_usd),0) s FROM tests").fetchone()
        q = self._run("SELECT COUNT(*) c FROM tests WHERE status IN ('queued','running')").fetchone()
        return {"by_verdict": {r["verdict"]: r["c"] for r in rows}, "total": total["c"],
                "spent_usd": round(total["s"], 2), "active": q["c"]}

    # --- launches ----------------------------------------------------------------
    LAUNCH_COLS = ("name", "ticker", "description", "image_path", "video_url", "video_path", "github", "website",
                   "app_url", "docs_url", "x_url", "telegram", "onboarding", "creator_wallet")

    def create_launch(self, data: dict) -> str:
        lid = uuid.uuid4().hex[:12]
        cols = ["id", "status", "created_at"] + list(self.LAUNCH_COLS)
        vals = [lid, "auditing", time.time()] + [data.get(c) for c in self.LAUNCH_COLS]
        self._run(f"INSERT INTO launches ({','.join(cols)}) VALUES ({','.join('?' * len(cols))})", tuple(vals))
        return lid

    def update_launch(self, lid: str, **fields) -> None:
        if not fields:
            return
        sets = ", ".join(f"{k}=?" for k in fields)
        self._run(f"UPDATE launches SET {sets} WHERE id=?", tuple(fields.values()) + (lid,))

    def get_launch(self, lid: str) -> dict | None:
        r = self._run("SELECT * FROM launches WHERE id=?", (lid,)).fetchone()
        return dict(r) if r else None

    def launch_by_mint(self, mint: str) -> dict | None:
        r = self._run("SELECT * FROM launches WHERE mint=? ORDER BY created_at DESC LIMIT 1", (mint,)).fetchone()
        return dict(r) if r else None

    def launches(self, status: str | None = None, limit: int = 50) -> list[dict]:
        if status:
            rows = self._run("SELECT * FROM launches WHERE status=? ORDER BY COALESCE(launched_at, created_at) DESC LIMIT ?",
                             (status, limit)).fetchall()
        else:
            rows = self._run("SELECT * FROM launches ORDER BY created_at DESC LIMIT ?", (limit,)).fetchall()
        return [dict(r) for r in rows]

    def rekey_test(self, tid: str, key: str, ca: str) -> None:
        self._run("UPDATE tests SET key=?, ca=? WHERE id=?", (key, ca, tid))

    # --- x bot state ------------------------------------------------------------
    def get_state(self, k: str) -> str | None:
        r = self._run("SELECT v FROM x_state WHERE k=?", (k,)).fetchone()
        return r["v"] if r else None

    def set_state(self, k: str, v: str) -> None:
        self._run("INSERT INTO x_state(k,v) VALUES(?,?) ON CONFLICT(k) DO UPDATE SET v=excluded.v", (k, v))
