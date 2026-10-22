"""Result persistence (SQLite, separate from the PostgreSQL system under test)."""

from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path

from gauntlet.config import RESULTS_DB

SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
    id TEXT PRIMARY KEY,
    created_at TEXT NOT NULL,
    benchmark_version TEXT NOT NULL,
    scenario_id TEXT NOT NULL,
    impl_id TEXT NOT NULL,
    impl_kind TEXT NOT NULL,
    seed INTEGER NOT NULL,
    status TEXT NOT NULL,            -- queued | running | completed
    verdict TEXT,                    -- pass | fail | inconclusive | infra_error
    schedule_hash TEXT,
    trace_hash TEXT,
    outcome_hash TEXT,
    duration_ms REAL,
    benchmark_id TEXT,
    summary_json TEXT,
    data_json TEXT
);
CREATE INDEX IF NOT EXISTS runs_created ON runs(created_at);
CREATE TABLE IF NOT EXISTS benchmarks (
    id TEXT PRIMARY KEY,
    created_at TEXT NOT NULL,
    finished_at TEXT,
    status TEXT NOT NULL,
    config_json TEXT NOT NULL,
    summary_json TEXT
);
-- Differential comparisons, schedule explorations, minimizations and experiments (added after v0.3.0;
-- created on demand, so older result databases stay readable).
CREATE TABLE IF NOT EXISTS analyses (
    id TEXT PRIMARY KEY,
    kind TEXT NOT NULL,              -- differential | exploration | minimization | experiment
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    status TEXT NOT NULL,
    config_json TEXT NOT NULL,
    result_json TEXT
);
CREATE INDEX IF NOT EXISTS analyses_kind ON analyses(kind, created_at);
"""

_SUMMARY_KEYS = ("id", "created_at", "benchmark_version", "scenario_id", "impl_id", "impl_kind", "strategy",
                 "seed", "verdict", "schedule_hash", "trace_hash", "outcome_hash", "duration_ms", "metrics",
                 "infra_error", "inconclusive", "origin", "schedule_source", "error_class")


class Store:
    def __init__(self, path: Path | str = RESULTS_DB):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        with self._conn() as c:
            c.executescript(SCHEMA)

    def _conn(self) -> sqlite3.Connection:
        c = sqlite3.connect(self.path, timeout=30)
        c.row_factory = sqlite3.Row
        return c

    # -- runs ------------------------------------------------------------------
    def create_pending(self, run_id: str, created_at: str, version: str, scenario_id: str, impl_id: str,
                       impl_kind: str, seed: int, benchmark_id: str | None = None) -> None:
        with self._lock, self._conn() as c:
            c.execute("INSERT INTO runs (id, created_at, benchmark_version, scenario_id, impl_id, impl_kind, seed, "
                      "status, benchmark_id) VALUES (?,?,?,?,?,?,?, 'queued', ?)",
                      (run_id, created_at, version, scenario_id, impl_id, impl_kind, seed, benchmark_id))

    def mark_running(self, run_id: str) -> None:
        with self._lock, self._conn() as c:
            c.execute("UPDATE runs SET status = 'running' WHERE id = ?", (run_id,))

    def save_run(self, run: dict, benchmark_id: str | None = None, run_id: str | None = None) -> str:
        rid = run_id or run["id"]
        run = {**run, "id": rid}
        summary = {k: run.get(k) for k in _SUMMARY_KEYS}
        summary["failed_invariants"] = [r["id"] for r in run["invariants"] if r["status"] == "fail"]
        with self._lock, self._conn() as c:
            c.execute(
                "INSERT INTO runs (id, created_at, benchmark_version, scenario_id, impl_id, impl_kind, seed, status, "
                "verdict, schedule_hash, trace_hash, outcome_hash, duration_ms, benchmark_id, summary_json, data_json) "
                "VALUES (?,?,?,?,?,?,?, 'completed', ?,?,?,?,?,?,?,?) "
                "ON CONFLICT(id) DO UPDATE SET status='completed', verdict=excluded.verdict, "
                "schedule_hash=excluded.schedule_hash, trace_hash=excluded.trace_hash, "
                "outcome_hash=excluded.outcome_hash, duration_ms=excluded.duration_ms, "
                "summary_json=excluded.summary_json, data_json=excluded.data_json",
                (rid, run["created_at"], run["benchmark_version"], run["scenario_id"], run["impl_id"],
                 run["impl_kind"], run["seed"], run["verdict"], run["schedule_hash"], run["trace_hash"],
                 run["outcome_hash"], run["duration_ms"], benchmark_id,
                 json.dumps(summary, default=str), json.dumps(run, default=str)))
        return rid

    def mark_failed_to_start(self, run_id: str, message: str) -> None:
        summary = {"id": run_id, "verdict": "infra_error", "infra_error": message}
        with self._lock, self._conn() as c:
            c.execute("UPDATE runs SET status='completed', verdict='infra_error', summary_json=? WHERE id=?",
                      (json.dumps(summary), run_id))

    def list_runs(self, limit: int = 200, scenario_id: str | None = None, impl_id: str | None = None,
                  benchmark_id: str | None = None) -> list[dict]:
        q = ("SELECT id, created_at, benchmark_version, scenario_id, impl_id, impl_kind, seed, status, verdict, "
             "schedule_hash, trace_hash, outcome_hash, duration_ms, benchmark_id, summary_json FROM runs WHERE 1=1")
        args: list = []
        for col, val in (("scenario_id", scenario_id), ("impl_id", impl_id), ("benchmark_id", benchmark_id)):
            if val:
                q += f" AND {col} = ?"
                args.append(val)
        q += " ORDER BY created_at DESC LIMIT ?"
        args.append(limit)
        with self._conn() as c:
            rows = c.execute(q, args).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            s = json.loads(d.pop("summary_json") or "{}")
            d["metrics"] = s.get("metrics")
            d["failed_invariants"] = s.get("failed_invariants", [])
            d["infra_error"] = s.get("infra_error")
            d["origin"] = s.get("origin")
            d["schedule_source"] = s.get("schedule_source") or "catalog"
            out.append(d)
        return out

    def get_run(self, run_id: str) -> dict | None:
        with self._conn() as c:
            r = c.execute("SELECT status, verdict, data_json, summary_json, scenario_id, impl_id, seed, created_at "
                          "FROM runs WHERE id = ?", (run_id,)).fetchone()
        if r is None:
            return None
        if r["data_json"]:
            data = json.loads(r["data_json"])
            data["status"] = r["status"]
            return data
        summary = json.loads(r["summary_json"] or "{}")
        return {"id": run_id, "status": r["status"], "verdict": r["verdict"], "scenario_id": r["scenario_id"],
                "impl_id": r["impl_id"], "seed": r["seed"], "created_at": r["created_at"],
                "infra_error": summary.get("infra_error")}

    def run_record_sha256(self, run_id: str) -> str | None:
        """SHA-256 of a run's stored record (data_json, or summary_json if it never completed)."""
        with self._conn() as c:
            r = c.execute("SELECT data_json, summary_json FROM runs WHERE id = ?", (run_id,)).fetchone()
        if r is None:
            return None
        return hashlib.sha256((r["data_json"] or r["summary_json"] or "").encode("utf-8")).hexdigest()

    # -- analyses (differential / exploration / minimization / experiment) -----------
    def create_analysis(self, kind: str, config: dict, status: str = "queued", analysis_id: str | None = None) -> str:
        aid = analysis_id or f"{kind[:4]}-{uuid.uuid4().hex[:12]}"
        now = datetime.now(timezone.utc).isoformat()
        with self._lock, self._conn() as c:
            c.execute("INSERT INTO analyses (id, kind, created_at, updated_at, status, config_json) VALUES (?,?,?,?,?,?)",
                      (aid, kind, now, now, status, json.dumps(config, default=str)))
        return aid

    def update_analysis(self, analysis_id: str, *, status: str | None = None, result: dict | None = None,
                        config: dict | None = None) -> None:
        now = datetime.now(timezone.utc).isoformat()
        sets, args = ["updated_at = ?"], [now]
        if config is not None:
            sets.append("config_json = ?")
            args.append(json.dumps(config, default=str))
        if status is not None:
            sets.append("status = ?")
            args.append(status)
        if result is not None:
            sets.append("result_json = ?")
            args.append(json.dumps(result, default=str))
        with self._lock, self._conn() as c:
            c.execute(f"UPDATE analyses SET {', '.join(sets)} WHERE id = ?", (*args, analysis_id))

    def get_analysis(self, analysis_id: str) -> dict | None:
        with self._conn() as c:
            r = c.execute("SELECT * FROM analyses WHERE id = ?", (analysis_id,)).fetchone()
        if r is None:
            return None
        d = dict(r)
        d["config"] = json.loads(d.pop("config_json"))
        d["result"] = json.loads(d.pop("result_json") or "null")
        return d

    def list_analyses(self, kind: str | None = None, limit: int = 200) -> list[dict]:
        q = "SELECT id, kind, created_at, updated_at, status, config_json FROM analyses"
        args: list = []
        if kind:
            q += " WHERE kind = ?"
            args.append(kind)
        q += " ORDER BY created_at DESC LIMIT ?"
        args.append(limit)
        with self._conn() as c:
            rows = c.execute(q, args).fetchall()
        return [{**{k: r[k] for k in ("id", "kind", "created_at", "updated_at", "status")},
                 "config": json.loads(r["config_json"])} for r in rows]

    def interrupt_unfinished_analyses(self, message: str) -> list[str]:
        """Analyses left queued/running by a previous process can never finish: mark them interrupted."""
        with self._conn() as c:
            ids = [r[0] for r in c.execute("SELECT id FROM analyses WHERE status IN ('queued', 'running')")]
        for aid in ids:
            a = self.get_analysis(aid)
            res = a["result"] or {}
            res.setdefault("notes", []).append(message)
            res["stop_reason"] = res.get("stop_reason") or "interrupted"
            self.update_analysis(aid, status="interrupted", result=res)
        return ids

    # -- benchmarks --------------------------------------------------------------
    def create_benchmark(self, bench_id: str, created_at: str, config: dict) -> None:
        with self._lock, self._conn() as c:
            c.execute("INSERT INTO benchmarks (id, created_at, status, config_json) VALUES (?,?, 'running', ?)",
                      (bench_id, created_at, json.dumps(config)))

    def finish_benchmark(self, bench_id: str, finished_at: str, summary: dict, status: str = "completed") -> None:
        with self._lock, self._conn() as c:
            c.execute("UPDATE benchmarks SET finished_at=?, status=?, summary_json=? WHERE id=?",
                      (finished_at, status, json.dumps(summary, default=str), bench_id))

    def list_benchmarks(self) -> list[dict]:
        with self._conn() as c:
            rows = c.execute("SELECT id, created_at, finished_at, status, config_json FROM benchmarks "
                             "ORDER BY created_at DESC").fetchall()
        return [{**dict(r), "config": json.loads(r["config_json"])} for r in rows]

    def get_benchmark(self, bench_id: str | None = None) -> dict | None:
        with self._conn() as c:
            if bench_id:
                r = c.execute("SELECT * FROM benchmarks WHERE id = ?", (bench_id,)).fetchone()
            else:
                r = c.execute("SELECT * FROM benchmarks WHERE status = 'completed' "
                              "ORDER BY finished_at DESC LIMIT 1").fetchone()
        if r is None:
            return None
        d = dict(r)
        d["config"] = json.loads(d.pop("config_json"))
        d["summary"] = json.loads(d.pop("summary_json") or "null")
        return d
