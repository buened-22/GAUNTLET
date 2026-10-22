"""API for differential comparisons, schedule explorations, minimizations, replays and experiments.

Long analyses run on a dedicated single-worker job executor; their progress and final result are
persisted in the ``analyses`` table, so the API only ever reports what is stored. Cancellation is
cooperative (checked between executions). Implementation code still runs only in actor subprocesses.
"""

from __future__ import annotations

import threading
import traceback
from concurrent.futures import ThreadPoolExecutor
from typing import Callable

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import PlainTextResponse
from pydantic import BaseModel, Field, field_validator

from gauntlet.differential import NORMALIZATION_RULES, replay_differential, run_differential, validate_request
from gauntlet.execution import outcome_class, replay
from gauntlet.experiment import export_bundle, generate_report, new_experiment, revise_experiment, verify_manifest
from gauntlet.explore import DEFAULT_LIMITS, canonical_schedule, explore
from gauntlet.impls.registry import is_known
from gauntlet.minimize import DEFAULT_BUDGET, minimize
from gauntlet.scenarios.fixtures import FIXTURES
from gauntlet.schedules import resolve_scenario
from gauntlet.store import Store

SEED = Field(default=1, ge=0, le=2**31 - 1)
# Wall-clock limit for single runs and replays started through the API (normal runs take 3-20 s). It bounds a
# stalled execution so the worker is freed; the run is then recorded as a timeout, never as a pass.
API_RUN_TIME_LIMIT_S = 600
CANCELLABLE = ("exploration", "minimization", "evaluation")  # jobs that check for cancellation between executions


def _known_scenario(v: str) -> str:
    try:
        resolve_scenario(v)
    except KeyError:
        raise ValueError(f"unknown scenario {v!r}") from None
    return v


def _known_impl(v: str) -> str:
    if not is_known(v):
        raise ValueError(f"unknown implementation {v!r}")
    return v


class DifferentialRequest(BaseModel):
    impl_ids: list[str] = Field(min_length=2, max_length=10)
    scenario_id: str
    seed: int = SEED
    schedule_from_run: str | None = None  # use that persisted run's exact schedule
    time_limit_s: float = Field(default=180, gt=0, le=3600)

    @field_validator("scenario_id")
    @classmethod
    def _scenario(cls, v: str) -> str:
        return _known_scenario(v)


class ExplorationLimits(BaseModel):
    max_schedules: int = Field(default=DEFAULT_LIMITS["max_schedules"], ge=1, le=500)
    max_preemptions: int = Field(default=DEFAULT_LIMITS["max_preemptions"], ge=0, le=3)
    max_depth: int = Field(default=DEFAULT_LIMITS["max_depth"], ge=1, le=5000)
    max_events_per_run: int = Field(default=DEFAULT_LIMITS["max_events_per_run"], ge=50, le=50_000)
    run_time_limit_s: float = Field(default=DEFAULT_LIMITS["run_time_limit_s"], gt=0, le=3600)
    max_duration_s: float = Field(default=DEFAULT_LIMITS["max_duration_s"], gt=0, le=24 * 3600)
    max_retries: int = Field(default=DEFAULT_LIMITS["max_retries"], ge=0, le=5)


class ExplorationRequest(BaseModel):
    scenario_id: str
    impl_id: str
    seed: int = SEED
    directive_index: int | None = Field(default=None, ge=0)
    limits: ExplorationLimits = ExplorationLimits()

    @field_validator("scenario_id")
    @classmethod
    def _scenario(cls, v: str) -> str:
        return _known_scenario(v)

    @field_validator("impl_id")
    @classmethod
    def _impl(cls, v: str) -> str:
        return _known_impl(v)


class MinimizationBudget(BaseModel):
    max_attempts: int = Field(default=DEFAULT_BUDGET["max_attempts"], ge=1, le=1000)
    max_duration_s: float = Field(default=DEFAULT_BUDGET["max_duration_s"], gt=0, le=24 * 3600)
    run_time_limit_s: float = Field(default=DEFAULT_BUDGET["run_time_limit_s"], gt=0, le=3600)
    reproduce_repeats: int = Field(default=DEFAULT_BUDGET["reproduce_repeats"], ge=1, le=10)
    confirm_repeats: int = Field(default=DEFAULT_BUDGET["confirm_repeats"], ge=1, le=10)
    max_schedule_directives: int = Field(default=DEFAULT_BUDGET["max_schedule_directives"], ge=1, le=100_000)
    orders_policy: str = Field(default="intersect", pattern="^(intersect|any)$")


class MinimizationRequest(BaseModel):
    run_id: str
    invariant: str | None = None
    budget: MinimizationBudget = MinimizationBudget()


class IsolationRequest(BaseModel):
    mode: str = Field(default="process", pattern="^(process|strict)$")
    memory_mb: int = Field(default=512, ge=64, le=8192)
    cpu_seconds: int = Field(default=120, ge=1, le=3600)
    output_limit_bytes: int = Field(default=1_000_000, ge=1024, le=100_000_000)
    trusted_environment: bool = False  # required for process mode (not a security boundary)


class EvaluationRequest(BaseModel):
    candidate_id: str = Field(pattern="^cand-[0-9a-f]{16}$")
    scenarios: list[str] | None = None
    seeds: list[int] = Field(default=[1], min_length=1, max_length=10)
    references: list[str] | None = None
    isolation: IsolationRequest = IsolationRequest()
    run_time_limit_s: float = Field(default=180, gt=0, le=3600)
    explore: ExplorationLimits | None = ExplorationLimits(max_schedules=40, max_preemptions=1)
    minimize_max_attempts: int | None = Field(default=40, ge=1, le=500)

    @field_validator("scenarios")
    @classmethod
    def _scenarios(cls, v):
        return [_known_scenario(s) for s in v] if v else v


class ExperimentRequest(BaseModel):
    config: dict


class ExperimentRevision(BaseModel):
    changes: dict
    reason: str | None = None


def IMPLEMENTATIONS_REF() -> set[str]:
    from gauntlet.impls.registry import IMPLEMENTATIONS
    return {i for i, info in IMPLEMENTATIONS.items() if info.kind == "reference"}


def register(app: FastAPI, st: Callable[[], Store], jobs: ThreadPoolExecutor) -> None:
    cancel_flags: set[str] = set()
    lock = threading.Lock()
    st().interrupt_unfinished_analyses("server stopped before the analysis completed")

    def submit(aid: str, fn: Callable[[], None]) -> None:
        def work() -> None:
            try:
                st().update_analysis(aid, status="running")
                fn()
            except Exception as exc:  # never leave an analysis silently 'running'
                a = st().get_analysis(aid)
                res = (a or {}).get("result") or {}
                res.update(stop_reason=f"{type(exc).__name__}: {exc}", error=traceback.format_exc(limit=4))
                st().update_analysis(aid, status="error", result=res)
            finally:
                with lock:
                    cancel_flags.discard(aid)
        jobs.submit(work)

    def cancelled(aid: str) -> Callable[[], bool]:
        return lambda: aid in cancel_flags

    def get(aid: str, kind: str | None = None) -> dict:
        a = st().get_analysis(aid)
        if a is None or (kind and a["kind"] != kind):
            raise HTTPException(404, f"no {kind or 'analysis'} {aid}")
        return a

    @app.get("/api/fixtures")
    def fixtures() -> list[dict]:
        return [{**f.to_dict(), "fixture": True} for f in FIXTURES.values()]

    @app.get("/api/analyses")
    def list_analyses(kind: str | None = Query(None, pattern="^(differential|exploration|minimization|experiment|replay|evaluation)$"),
                      limit: int = Query(100, ge=1, le=1000)) -> list[dict]:
        out = []
        for a in st().list_analyses(kind, limit):
            cfg = a["config"]
            a["config"] = {k: v for k, v in cfg.items() if k not in ("schedule", "base_schedule", "versions")} | (
                {"title": cfg["versions"][-1]["config"]["title"]} if "versions" in cfg else {})
            out.append(a)
        return out

    @app.post("/api/analyses/{aid}/cancel")
    def cancel(aid: str) -> dict:
        a = get(aid)
        if a["kind"] not in CANCELLABLE:
            raise HTTPException(409, f"{a['kind']} jobs cannot be cancelled (each is a short, bounded set of executions)")
        if a["status"] not in ("queued", "running"):
            raise HTTPException(409, f"{aid} is {a['status']}")
        with lock:
            cancel_flags.add(aid)
        return {"id": aid, "cancel_requested": True}

    # -- differential ----------------------------------------------------------------------------
    @app.post("/api/differential", status_code=202)
    def start_differential(req: DifferentialRequest) -> dict:
        problems = validate_request(req.impl_ids, req.scenario_id)
        if problems:
            raise HTTPException(422, "; ".join(problems))
        schedule = None
        seed = req.seed
        if req.schedule_from_run:
            src = st().get_run(req.schedule_from_run)
            if src is None or "schedule" not in src:
                raise HTTPException(404, f"no completed run {req.schedule_from_run}")
            if src["scenario_id"] != req.scenario_id:
                raise HTTPException(422, "schedule_from_run belongs to a different scenario")
            schedule, seed = src["schedule"], src["seed"]
        aid = st().create_analysis("differential", {"impl_ids": req.impl_ids, "scenario_id": req.scenario_id,
                                                    "seed": seed, "schedule_from_run": req.schedule_from_run,
                                                    "time_limit_s": req.time_limit_s})
        submit(aid, lambda: run_differential(st(), req.impl_ids, req.scenario_id, seed, schedule=schedule,
                                             time_limit_s=req.time_limit_s, comparison_id=aid))
        return {"id": aid, "status": "queued"}

    @app.get("/api/differential/rules")
    def differential_rules() -> dict:
        return {"normalization_rules": NORMALIZATION_RULES,
                "classifications": {"agreement": "no normalized difference (does not prove correctness)",
                                    "confirmed_violation": "behaviour differs and the oracle FAILs an invariant in at least one run",
                                    "unconfirmed_disagreement": "behaviour differs, no run FAILs the oracle",
                                    "inconclusive": "an execution error/timeout, or a difference with only inconclusive oracle results"}}

    @app.get("/api/differential/{aid}")
    def get_differential(aid: str) -> dict:
        return get(aid, "differential")

    @app.post("/api/differential/{aid}/replay", status_code=202)
    def replay_comparison(aid: str) -> dict:
        a = get(aid, "differential")
        if a["status"] != "completed":
            raise HTTPException(409, f"{aid} is {a['status']}")
        rid = st().create_analysis("replay", {"of": aid, "kind": "differential"})

        def job():
            summary = replay_differential(st(), aid)
            st().update_analysis(rid, status="completed", result=summary)
        submit(rid, job)
        return {"id": rid, "status": "queued"}

    # -- generic run replay ----------------------------------------------------------------------
    @app.post("/api/runs/{run_id}/replay", status_code=202)
    def replay_run(run_id: str) -> dict:
        orig = st().get_run(run_id)
        if orig is None or orig.get("status") != "completed" or "schedule" not in orig:
            raise HTTPException(409, f"run {run_id} is not a completed run with a stored schedule")
        rid = st().create_analysis("replay", {"of_run": run_id})

        def job():
            r = replay(st(), orig, origin={"kind": "replay", "of": run_id, "analysis": rid},
                       time_limit_s=API_RUN_TIME_LIMIT_S)
            st().update_analysis(rid, status="completed",
                                 result={k: v for k, v in r.items() if k != "run"} | {"outcome": outcome_class(r["run"])})
        submit(rid, job)
        return {"id": rid, "status": "queued"}

    @app.get("/api/replays/{aid}")
    def get_replay(aid: str) -> dict:
        return get(aid, "replay")

    # -- exploration -----------------------------------------------------------------------------
    @app.post("/api/explorations", status_code=202)
    def start_exploration(req: ExplorationRequest) -> dict:
        aid = st().create_analysis("exploration", {"scenario_id": req.scenario_id, "impl_id": req.impl_id,
                                                   "seed": req.seed, "limits": req.limits.model_dump()})
        submit(aid, lambda: explore(st(), req.scenario_id, req.impl_id, req.seed, directive_index=req.directive_index,
                                    limits=req.limits.model_dump(), exploration_id=aid, cancel=cancelled(aid)))
        return {"id": aid, "status": "queued"}

    @app.get("/api/explorations/{aid}")
    def get_exploration(aid: str) -> dict:
        a = get(aid, "exploration")
        return {**a, "config": {k: v for k, v in a["config"].items() if k != "base_schedule"}}

    @app.get("/api/explorations/{aid}/schedules/{n}/export")
    def export_schedule(aid: str, n: int) -> dict:
        a = get(aid, "exploration")
        if not a["result"] or "base_schedule" not in a["config"]:
            raise HTTPException(409, "exploration has no executed schedules yet")
        entry = next((s for s in a["result"]["schedules"] if s["n"] == n), None)
        if entry is None:
            raise HTTPException(404, f"no schedule {n}")
        run = st().get_run(entry["run_id"])
        return {"exploration_id": aid, "n": n, "schedule_id": entry["schedule_id"],
                "schedule": canonical_schedule(a, n), "exploration_config": a["config"]["limits"],
                "directive_index": a["config"]["directive_index"], "run_id": entry["run_id"],
                "verdict": run["verdict"], "invariants": run["invariants"], "decisions": run.get("decisions"),
                "trace": run["events"], "replay": f"POST /api/runs/{entry['run_id']}/replay"}

    # -- minimization ----------------------------------------------------------------------------
    @app.post("/api/minimizations", status_code=202)
    def start_minimization(req: MinimizationRequest) -> dict:
        run = st().get_run(req.run_id)
        if run is None or run.get("status") != "completed" or "invariants" not in run:
            raise HTTPException(404, f"no completed run {req.run_id}")
        failed = [i["id"] for i in run["invariants"] if i["status"] == "fail"]
        if not failed or run.get("infra_error"):
            raise HTTPException(422, f"run {req.run_id} has no confirmed invariant violation")
        if req.invariant and req.invariant not in failed:
            raise HTTPException(422, f"{req.invariant} does not FAIL in run {req.run_id} (failed: {failed})")
        aid = st().create_analysis("minimization", {"run_id": req.run_id, "invariant": req.invariant or failed[0],
                                                    "budget": req.budget.model_dump()})
        submit(aid, lambda: minimize(st(), req.run_id, invariant=req.invariant, budget=req.budget.model_dump(),
                                     minimization_id=aid, cancel=cancelled(aid)))
        return {"id": aid, "status": "queued"}

    @app.get("/api/minimizations/{aid}")
    def get_minimization(aid: str) -> dict:
        return get(aid, "minimization")

    # -- experiments -----------------------------------------------------------------------------
    @app.post("/api/experiments", status_code=201)
    def create_experiment(req: ExperimentRequest) -> dict:
        try:
            return {"id": new_experiment(st(), req.config)}
        except (ValueError, KeyError) as exc:
            raise HTTPException(422, str(exc)) from exc

    @app.get("/api/experiments/{aid}")
    def get_experiment(aid: str) -> dict:
        return get(aid, "experiment")

    @app.put("/api/experiments/{aid}")
    def revise(aid: str, req: ExperimentRevision) -> dict:
        get(aid, "experiment")
        try:
            return revise_experiment(st(), aid, req.changes, req.reason)
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc

    @app.post("/api/experiments/{aid}/report")
    def report(aid: str) -> dict:
        get(aid, "experiment")
        try:
            return generate_report(st(), aid)
        except KeyError as exc:
            raise HTTPException(422, str(exc)) from exc

    @app.get("/api/experiments/{aid}/report.md", response_class=PlainTextResponse)
    def report_md(aid: str) -> str:
        get(aid, "experiment")
        return generate_report(st(), aid, record=False)["markdown"]

    # -- external candidates ---------------------------------------------------------------------
    # Registration from a directory is CLI-only (``gauntlet candidate register``): the API never accepts
    # host file-system paths. The API lists, inspects and evaluates already registered snapshots.
    @app.get("/api/isolation")
    def isolation_capabilities() -> dict:
        from gauntlet.candidates.sandbox import capabilities
        return capabilities()

    @app.get("/api/candidates")
    def list_candidates() -> list[dict]:
        from gauntlet.candidates.snapshots import list_candidates as lc
        return lc()

    @app.get("/api/candidates/{cid}")
    def get_candidate(cid: str) -> dict:
        from gauntlet.candidates.snapshots import CandidateError, load_meta, verify
        try:
            meta = load_meta(cid)
        except CandidateError as exc:
            raise HTTPException(404, str(exc)) from exc
        try:
            verify(cid)
            integrity = {"ok": True}
        except CandidateError as exc:
            integrity = {"ok": False, "error": str(exc)}
        return {**{k: v for k, v in meta.items() if k != "source_dir_name"}, "integrity": integrity}

    @app.post("/api/evaluations", status_code=202)
    def start_evaluation(req: EvaluationRequest) -> dict:
        from gauntlet.candidates.evaluate import evaluate
        from gauntlet.candidates.snapshots import CandidateError, load_meta
        try:
            load_meta(req.candidate_id)
        except CandidateError as exc:
            raise HTTPException(404, str(exc)) from exc
        refs = req.references
        if refs and any(r not in IMPLEMENTATIONS_REF() for r in refs):
            raise HTTPException(422, f"references must be among {sorted(IMPLEMENTATIONS_REF())}")
        aid = st().create_analysis("evaluation", {"candidate_id": req.candidate_id})
        explore_cfg = False if req.explore is None else {"max_schedules": req.explore.max_schedules,
                                                         "max_preemptions": req.explore.max_preemptions}
        minimize_cfg = False if req.minimize_max_attempts is None else {"max_attempts": req.minimize_max_attempts}
        submit(aid, lambda: evaluate(st(), req.candidate_id, scenarios=req.scenarios, seeds=req.seeds, references=refs,
                                     isolation=req.isolation.model_dump(), run_time_limit_s=req.run_time_limit_s,
                                     explore_cfg=explore_cfg, minimize_cfg=minimize_cfg, evaluation_id=aid,
                                     cancel=cancelled(aid)))
        return {"id": aid, "status": "queued"}

    @app.get("/api/evaluations/{aid}")
    def get_evaluation(aid: str) -> dict:
        return get(aid, "evaluation")

    @app.get("/api/evaluations/{aid}/summary.md", response_class=PlainTextResponse)
    def evaluation_summary(aid: str) -> str:
        a = get(aid, "evaluation")
        md = (a["result"] or {}).get("summary_markdown")
        if not md:
            raise HTTPException(409, f"evaluation {aid} has no summary ({a['status']})")
        return md

    @app.get("/api/experiments/{aid}/verify")
    def verify(aid: str) -> dict:
        """Re-hash the evidence named by the latest report's manifest (missing / modified artifacts)."""
        a = get(aid, "experiment")
        man = (a["result"] or {}).get("latest_manifest")
        if not man:
            raise HTTPException(409, "no report has been generated for this experiment yet")
        return verify_manifest(st(), man)

    @app.get("/api/experiments/{aid}/export")
    def export(aid: str) -> dict:
        get(aid, "experiment")
        return export_bundle(st(), aid)
