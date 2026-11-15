"""FastAPI backend. Runs execute on a single background worker (one at a time);
implementation code runs only inside actor subprocesses, never in this process.

The API has no authentication and is for local use only. ``gauntlet.api.local_guard`` refuses requests that
are not addressed to a loopback name or that come from a foreign web origin."""

from __future__ import annotations

import threading
import traceback
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import PlainTextResponse
from pydantic import BaseModel, Field, field_validator

from gauntlet import BENCHMARK_VERSION
from gauntlet.compare import compare_runs
from gauntlet.engine.runner import execute_run, schedule_definition
from gauntlet.faults import FAULTS
from gauntlet.impls.registry import IMPLEMENTATIONS, get_info
from gauntlet.oracle.invariants import INVARIANTS
from gauntlet.api.analyses import API_RUN_TIME_LIMIT_S, register as register_analyses
from gauntlet.api.local_guard import LocalRequestGuard, allowed_origins
from gauntlet.pg.provision import InfrastructureError, check_server
from gauntlet.report import benchmark_report, run_report
from gauntlet.scenarios.catalog import SCENARIOS
from gauntlet.store import Store


class RunRequest(BaseModel):
    scenario_id: str
    impl_id: str
    seed: int = Field(default=1, ge=0, le=2**31 - 1)

    @field_validator("scenario_id")
    @classmethod
    def _scenario(cls, v: str) -> str:
        if v not in SCENARIOS:
            raise ValueError(f"unknown scenario {v!r}")
        return v

    @field_validator("impl_id")
    @classmethod
    def _impl(cls, v: str) -> str:
        if v not in IMPLEMENTATIONS:
            raise ValueError(f"unknown implementation {v!r}")
        return v


def create_app(store: Store | None = None, executor: ThreadPoolExecutor | None = None,
               jobs: ThreadPoolExecutor | None = None) -> FastAPI:
    app = FastAPI(title="Gauntlet API", version=BENCHMARK_VERSION)
    app.add_middleware(CORSMiddleware, allow_origins=allowed_origins(), allow_methods=["*"], allow_headers=["*"])
    # Added last, so it runs first: a request relayed by a web page in the operator's browser (DNS rebinding,
    # cross-site request) is refused before it reaches CORS handling or any route (gauntlet.api.local_guard).
    app.add_middleware(LocalRequestGuard)
    app.state.store = store or Store()
    app.state.executor = executor or ThreadPoolExecutor(max_workers=1, thread_name_prefix="gauntlet-run")
    app.state.active: dict[str, str] = {}
    lock = threading.Lock()

    # Runs left queued/running by a previous server process can never finish.
    for r in app.state.store.list_runs(limit=10_000):
        if r["status"] in ("queued", "running"):
            app.state.store.mark_failed_to_start(r["id"], "server stopped before the run completed")

    def st() -> Store:
        return app.state.store

    # Differential comparisons, explorations, minimizations, replays and experiments (gauntlet.api.analyses).
    app.state.jobs = jobs or ThreadPoolExecutor(max_workers=1, thread_name_prefix="gauntlet-job")
    register_analyses(app, st, app.state.jobs)

    @app.get("/api/health")
    def health() -> dict:
        try:
            pg = check_server()
            return {"ok": True, "postgres": pg}
        except InfrastructureError as exc:
            return {"ok": False, "postgres": None, "error": str(exc)}

    @app.get("/api/meta")
    def meta() -> dict:
        runs = st().list_runs(limit=1)
        bm = st().get_benchmark()
        headline = None
        if bm and bm.get("summary"):
            s = bm["summary"]
            headline = {k: s.get(k) for k in (
                "benchmark_id", "runs_completed", "verdicts", "invariant_violations", "mutants_total",
                "mutants_detected", "mutants_killed_as_intended", "inconclusive_runs", "infrastructure_failures",
                "scenarios_executed", "seeds")}
            headline["reproducibility"] = {k: s["reproducibility"][k] for k in ("checked", "reproduced")}
            headline["finished_at"] = bm.get("finished_at")
        return {
            "benchmark_version": BENCHMARK_VERSION,
            "counts": {"scenarios": len(SCENARIOS), "implementations": len(IMPLEMENTATIONS),
                       "references": sum(1 for i in IMPLEMENTATIONS.values() if i.kind == "reference"),
                       "mutants": sum(1 for i in IMPLEMENTATIONS.values() if i.kind == "mutant"),
                       "invariants": len(INVARIANTS), "faults": len(FAULTS)},
            "latest_run": runs[0] if runs else None,
            "latest_benchmark": headline,
            "active_runs": len(app.state.active),
        }

    @app.get("/api/scenarios")
    def scenarios() -> list[dict]:
        return [s.to_dict() for s in SCENARIOS.values()]

    @app.get("/api/scenarios/{scenario_id}")
    def scenario(scenario_id: str, seed: int = Query(1, ge=0, le=2**31 - 1)) -> dict:
        sc = SCENARIOS.get(scenario_id)
        if sc is None:
            raise HTTPException(404, f"unknown scenario {scenario_id}")
        sched = schedule_definition(scenario_id, seed)
        from gauntlet.engine.runner import canonical_hash
        return {**sc.to_dict(), "schedule_preview": {"seed": seed, "schedule_hash": canonical_hash(sched),
                                                    "initial_orders": sched["initial_orders"],
                                                    "directives": sched["directives"], "ops": sched["ops"]}}

    @app.get("/api/implementations")
    def implementations() -> list[dict]:
        return [i.to_dict() for i in IMPLEMENTATIONS.values()]

    @app.get("/api/invariants")
    def invariants() -> list[dict]:
        return [i.to_dict() for i in INVARIANTS]

    @app.get("/api/faults")
    def faults() -> list[dict]:
        return FAULTS

    @app.post("/api/runs", status_code=202)
    def start_run(req: RunRequest) -> dict:
        run_id = uuid.uuid4().hex[:16]
        created = datetime.now(timezone.utc).isoformat()
        info = get_info(req.impl_id)
        st().create_pending(run_id, created, BENCHMARK_VERSION, req.scenario_id, req.impl_id, info.kind, req.seed)
        with lock:
            app.state.active[run_id] = "queued"

        def work() -> None:
            try:
                st().mark_running(run_id)
                with lock:
                    app.state.active[run_id] = "running"
                result = execute_run(req.scenario_id, req.impl_id, req.seed, time_limit_s=API_RUN_TIME_LIMIT_S)
                result["created_at"] = created
                st().save_run(result, run_id=run_id)
            except Exception as exc:  # never leave a run silently 'running'
                st().mark_failed_to_start(run_id, f"{type(exc).__name__}: {exc}\n{traceback.format_exc(limit=4)}")
            finally:
                with lock:
                    app.state.active.pop(run_id, None)

        app.state.executor.submit(work)
        return {"id": run_id, "status": "queued"}

    @app.get("/api/runs")
    def list_runs(limit: int = Query(200, ge=1, le=5000), scenario_id: str | None = None,
                  impl_id: str | None = None, benchmark_id: str | None = None) -> list[dict]:
        return st().list_runs(limit=limit, scenario_id=scenario_id, impl_id=impl_id, benchmark_id=benchmark_id)

    def _get(run_id: str, require_complete: bool = False) -> dict:
        run = st().get_run(run_id)
        if run is None:
            raise HTTPException(404, f"no run {run_id}")
        if require_complete and (run.get("status") != "completed" or "invariants" not in run):
            raise HTTPException(409, f"run {run_id} is not completed (status={run.get('status')}, "
                                     f"verdict={run.get('verdict')})")
        return run

    @app.get("/api/runs/{run_id}")
    def get_run(run_id: str) -> dict:
        run = _get(run_id)
        return {k: v for k, v in run.items() if k not in ("events", "ledger", "schedule", "decisions")}

    @app.get("/api/runs/{run_id}/trace")
    def trace(run_id: str) -> dict:
        run = _get(run_id, require_complete=True)
        return {"id": run_id, "events": run["events"], "attempts": run["attempts"], "snapshots": run["snapshots"],
                "directives": run["schedule"]["directives"], "decisions": run.get("decisions") or {}}

    @app.get("/api/runs/{run_id}/invariants")
    def run_invariants(run_id: str) -> list[dict]:
        return _get(run_id, require_complete=True)["invariants"]

    @app.get("/api/runs/{run_id}/report", response_class=PlainTextResponse)
    def report(run_id: str) -> str:
        return run_report(_get(run_id, require_complete=True))

    @app.get("/api/compare")
    def compare(a: str, b: str) -> dict:
        return compare_runs(_get(a, require_complete=True), _get(b, require_complete=True))

    @app.get("/api/benchmarks")
    def benchmarks() -> list[dict]:
        return st().list_benchmarks()

    @app.get("/api/benchmarks/latest")
    def latest_benchmark() -> dict:
        bm = st().get_benchmark()
        if bm is None:
            raise HTTPException(404, "no completed benchmark yet; run `python -m gauntlet bench`")
        return bm

    @app.get("/api/benchmarks/latest/report", response_class=PlainTextResponse)
    def latest_benchmark_report() -> str:
        bm = st().get_benchmark()
        if bm is None:
            raise HTTPException(404, "no completed benchmark yet")
        return benchmark_report(bm["summary"])

    return app


app = create_app()
