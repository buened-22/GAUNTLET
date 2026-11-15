"""End-to-end execution of one benchmark run.

    scenario.build(seed) -> fresh PG database with the P0 schema -> controller
    executes the directives -> settle -> final snapshot -> independent oracle
    -> hashes -> result dict (persisted by the caller).
"""

from __future__ import annotations

import contextlib
import copy
import hashlib
import json
import platform
import sys
import threading
import time
import traceback
import uuid
from datetime import datetime, timezone

import psycopg

from gauntlet import BENCHMARK_VERSION
from gauntlet.config import PG_ADMIN_DSN
from gauntlet.actor.proxy import ActorFailure, ProcessPool, StepTimeout
from gauntlet.engine.controller import Controller, RunLimitExceeded, RunTimeout, ScheduleError
from gauntlet.impls.registry import get_info
from gauntlet.oracle.checker import RunOracle, infra_results, verdict
from gauntlet.engine.observe import ObservationObstructed
from gauntlet.pg.provision import (
    InfrastructureError, apply_legacy_schema, check_server, create_candidate_role, create_observer_role,
    create_run_database, drop_candidate_role, drop_run_database, grant_observer_read,
)
from gauntlet.scenarios.catalog import SCENARIOS, get_scenario

# Trace fields that are observational (timing, OS identifiers) and therefore
# excluded from the logical-trace hash.
_NON_LOGICAL = {"t_ms", "blockers", "backend_pid"}


def canonical_hash(obj) -> str:
    return hashlib.sha256(json.dumps(obj, sort_keys=True, separators=(",", ":"), default=str).encode()).hexdigest()


def schedule_definition(scenario_id: str, seed: int) -> dict:
    """The schedule a catalog (or fixture) scenario defines for a seed."""
    if scenario_id in SCENARIOS:
        sc = get_scenario(scenario_id)
    else:
        from gauntlet.schedules import resolve_scenario
        sc = resolve_scenario(scenario_id)
    built = sc.build(seed)
    return {"benchmark_version": BENCHMARK_VERSION, "scenario_id": scenario_id, "seed": seed,
            "initial_orders": {str(k): v for k, v in sorted(built["initial_orders"].items())},
            "directives": built["directives"], "ops": built["ops"]}


def open_monitor(dsn: str) -> psycopg.Connection:
    """The harness's own observation connection, with a bounded lock wait (see MONITOR_LOCK_TIMEOUT_MS)."""
    from gauntlet import config
    conn = psycopg.connect(dsn, autocommit=True, application_name="gauntlet:monitor")
    conn.execute(f"SET lock_timeout = {int(config.MONITOR_LOCK_TIMEOUT_MS)}")
    return conn


# Session settings of the restricted observer (audit finding A-02), passed as startup options: they take
# precedence over per-database and per-role defaults, which the candidate (owner of the run database) can change.
_PINNED_PATH = "-c search_path=pg_catalog,pg_temp"


def open_observer(dsn: str) -> psycopg.Connection:
    """The snapshot connection of an external candidate's run: the per-run observer role (not a superuser),
    ``search_path`` pinned to the catalog, ``row_security = off`` (row-level security becomes an error instead
    of silent filtering), read-only transactions by default and a bounded lock wait. Verified after connecting."""
    from gauntlet import config
    options = (f"{_PINNED_PATH} -c row_security=off -c default_transaction_read_only=on "
               f"-c lock_timeout={int(config.MONITOR_LOCK_TIMEOUT_MS)}")
    conn = psycopg.connect(dsn, autocommit=True, application_name="gauntlet:observer", connect_timeout=10,
                           options=options)
    try:
        row = conn.execute("SELECT pg_catalog.current_setting('search_path'), pg_catalog.current_setting('row_security'), "
                           "r.rolsuper FROM pg_catalog.pg_roles r WHERE r.rolname = current_user").fetchone()
        if row is None or row[0].replace(" ", "") != "pg_catalog,pg_temp" or row[1] != "off" or row[2]:
            raise InfrastructureError(f"the observer session is not configured as required: {row}")
    except BaseException:
        conn.close()
        raise
    return conn


def open_control(admin_dsn: str) -> psycopg.Connection:
    """The control connection of an external candidate's run: lock probe, liveness, backend termination and
    statement activity. It connects to the admin DSN's database (the maintenance database), never to the run
    database, so it never resolves a name inside a database the candidate owns."""
    return psycopg.connect(admin_dsn, autocommit=True, application_name="gauntlet:control", connect_timeout=10,
                           options=_PINNED_PATH)


def logical_trace(events: list[dict]) -> list[dict]:
    return [{k: v for k, v in e.items() if k not in _NON_LOGICAL} for e in events]


def execute_run(scenario_id: str, impl_id: str, seed: int, admin_dsn: str = PG_ADMIN_DSN,
                keep_database: bool = False, *, schedule: dict | None = None, origin: dict | None = None,
                time_limit_s: float | None = None, max_events: int | None = None,
                isolation: dict | None = None) -> dict:
    """Execute one run on a fresh database and judge it with the independent oracle.

    Without ``schedule`` the scenario's own schedule for ``seed`` is executed (catalog behaviour,
    unchanged). With ``schedule`` (an explicit schedule document, see ``gauntlet.schedules``) that
    document is validated and executed instead; ``scenario_id``/``seed`` must match it.
    ``time_limit_s`` and ``max_events`` are enforced during execution; exceeding them ends the run as
    an infrastructure error (``timeout`` / ``limit_exceeded``), never as a pass.

    External candidates (``ext:<candidate_id>``) additionally run with: snapshot verification, the
    isolation check of ``isolation`` (fail closed), a harness-wide execution slot, sandboxed actor
    processes and a restricted per-run database role. A failure of the candidate's own actor process
    (crash, protocol violation, load failure) is classified ``candidate_execution_failure``; the run is
    never judged and never a pass.
    """
    from gauntlet.candidates import is_external
    from gauntlet.schedules import provenance, resolve_scenario, validate_schedule
    info = get_info(impl_id)
    schedule_problems: list[str] = []
    if schedule is None:
        scenario = resolve_scenario(scenario_id)
        schedule = schedule_definition(scenario_id, seed)
        source = "catalog" if scenario_id in SCENARIOS else "fixture"
    else:
        if schedule.get("scenario_id") != scenario_id or schedule.get("seed") != seed:
            raise ValueError("explicit schedule does not match scenario_id/seed")
        scenario = resolve_scenario(scenario_id)
        schedule = copy.deepcopy(schedule)
        source = schedule.get("source", "explicit")
        schedule_problems = validate_schedule(schedule)
    run = {
        "id": uuid.uuid4().hex[:16],
        "created_at": datetime.now(timezone.utc).isoformat(),
        "benchmark_version": BENCHMARK_VERSION,
        "scenario_id": scenario_id, "impl_id": impl_id, "impl_kind": info.kind, "strategy": info.strategy,
        "seed": seed,
        "config": {"scenario_id": scenario_id, "impl_id": impl_id, "seed": seed,
                   "benchmark_version": BENCHMARK_VERSION},
        "schedule": schedule,
        "schedule_hash": canonical_hash(schedule),
        "environment": {"python": sys.version.split()[0], "platform": platform.platform()},
        "initial_state": schedule["initial_orders"],
        "events": [], "ledger": [], "attempts": [], "snapshots": [], "repeats": [],
        "invariants": [], "infra_error": None, "inconclusive": [], "notes": [],
        "schedule_source": source, "origin": origin, "provenance": provenance(impl_id, schedule),
        "limits": {"time_limit_s": time_limit_s, "max_events": max_events},
        "error_class": None, "decisions": {}, "database": None,
    }
    external = is_external(impl_id)
    t0 = time.perf_counter()
    db = None
    ctl = None
    pool = None
    slot = None
    cand_role = None
    obs_role = None
    actor_dsn = None
    iso_cfg = None
    try:
        if schedule_problems:
            raise ScheduleError("invalid schedule: " + "; ".join(schedule_problems[:10]))
        if external:
            from gauntlet.candidates import candidate_id_of
            from gauntlet.candidates.sandbox import IsolationConfig, check, run_slot
            from gauntlet.candidates.snapshots import verify
            meta = verify(candidate_id_of(impl_id))  # CandidateError if the snapshot changed
            run["candidate"] = {k: meta[k] for k in ("candidate_id", "source_hash", "contract_version", "loader_version")}
            iso_cfg = IsolationConfig.from_dict(isolation)
            run["isolation"] = check(iso_cfg)  # IsolationUnavailable: fail closed, nothing executes
            slot = run_slot()
        server = check_server(admin_dsn)
        # Start every actor process this schedule will need, in parallel.
        pool = ProcessPool(sum(1 for d in schedule["directives"] if d["kind"] in ("spawn", "restart")),
                           sandbox=iso_cfg)
        run["environment"]["postgres"] = server["server_version"]
        if not server["is_pg16"]:
            run["notes"].append(f"server is not PostgreSQL 16 ({server['server_version']})")
        db = create_run_database(admin_dsn)
        run["database"] = db.name
        actor_dsn = db.dsn
        if external:
            cand_role, role_db = create_candidate_role(db, admin_dsn)
            actor_dsn = role_db.dsn
            # A-02: snapshots are read by a separate non-superuser observer role, never by a superuser session
            obs_role, obs_db = create_observer_role(db, admin_dsn)
            run["database_role"] = {"name": cand_role, "superuser": False, "owns": "its run database only",
                                    "observer": obs_role}
        apply_legacy_schema(actor_dsn, {int(k): v for k, v in schedule["initial_orders"].items()})
        if external:
            grant_observer_read(actor_dsn, obs_role)  # as the candidate role, before any candidate code runs
        with contextlib.ExitStack() as stack:
            if external:
                monitor = stack.enter_context(open_observer(obs_db.dsn))
                control = stack.enter_context(open_control(admin_dsn))
            else:  # built-in (trusted) implementations: unchanged
                monitor = stack.enter_context(open_monitor(db.dsn))
                control = None
            remaining = time_limit_s - (time.perf_counter() - t0) if time_limit_s else None
            deadline = time.monotonic() + remaining if remaining is not None else None
            ctl = Controller(impl_id, actor_dsn, monitor, copy.deepcopy(schedule["ops"]), pool,
                             deadline=deadline, max_events=max_events, control=control)
            # The deadline is checked at every engine step; the watchdog additionally kills the actors
            # if a single wait outlasts it, so a stuck step cannot hold the run indefinitely.
            watchdog = threading.Timer(max(remaining, 0), ctl.abort) if remaining is not None else None
            if watchdog:
                watchdog.daemon = True
                watchdog.start()
            try:
                ctl.execute(schedule["directives"])
                ctl.settle()
            finally:
                if watchdog:
                    watchdog.cancel()
                    # cancel() does not stop an abort() that already started: let it finish before shutdown
                    # touches the same actors (abort only kills processes, so this is short).
                    watchdog.join(timeout=30)
                ctl.shutdown()
    except InfrastructureError as exc:
        run["infra_error"] = str(exc)
        timed_out = time_limit_s is not None and (time.perf_counter() - t0) > time_limit_s
        if timed_out and not isinstance(exc, RunTimeout):
            run["infra_error"] = f"run exceeded its wall-clock limit of {time_limit_s}s ({exc})"
        run["error_class"] = ("timeout" if isinstance(exc, RunTimeout) or timed_out else
                              "limit_exceeded" if isinstance(exc, RunLimitExceeded) else
                              "schedule_error" if isinstance(exc, ScheduleError) or str(exc).startswith("schedule error")
                              # a statement over the step timeout: the candidate's only when the evidence says so
                              else "candidate_statement_timeout" if external and isinstance(exc, StepTimeout)
                              and exc.attributable and exc.loaded
                              # the candidate made its own database unobservable (A-02 observer)
                              else "candidate_execution_failure" if external and isinstance(exc, ObservationObstructed)
                              else "candidate_execution_failure" if external and isinstance(exc, ActorFailure) and exc.loaded
                              else "infrastructure")
        if isinstance(exc, StepTimeout):
            run["notes"].append("step timeout evidence: " + json.dumps(exc.evidence, sort_keys=True, default=str))
    except Exception as exc:  # harness bug (or a candidate admission failure): never report as pass
        admission = _candidate_admission_error(exc) if external else None
        if admission:
            run["infra_error"], run["error_class"] = admission
        else:
            run["infra_error"] = f"harness exception: {type(exc).__name__}: {exc}"
            run["error_class"] = "harness_exception"
            if time_limit_s is not None and (time.perf_counter() - t0) > time_limit_s:
                run["infra_error"] = f"run exceeded its wall-clock limit of {time_limit_s}s ({run['infra_error']})"
                run["error_class"] = "timeout"
            run["notes"].append(traceback.format_exc(limit=8))
    finally:
        # Each cleanup step runs even if an earlier one fails, so a failure here can never leak the run
        # database, the candidate role or the execution slot. Failures are recorded, not raised: they happen
        # after the run's observations are complete and do not change them.
        cleanup_errors: list[str] = []

        def attempt(what: str, fn):
            try:
                return fn()
            except Exception as exc:  # noqa: BLE001
                cleanup_errors.append(f"{what}: {type(exc).__name__}: {exc}")
                return None

        if pool is not None:
            attempt("close actor pool", pool.close)
        if db is not None and not keep_database:
            attempt("drop run database", lambda: drop_run_database(db, admin_dsn))
        if cand_role is not None and not keep_database \
                and attempt("drop candidate role", lambda: drop_candidate_role(cand_role, admin_dsn)) is False:
            run["notes"].append(f"candidate database role {cand_role} could not be removed")
        if obs_role is not None and not keep_database \
                and attempt("drop observer role", lambda: drop_candidate_role(obs_role, admin_dsn)) is False:
            run["notes"].append(f"observer database role {obs_role} could not be removed")
        if slot is not None:
            attempt("release execution slot", slot.release)
        run["cleanup_errors"] = cleanup_errors
        run["notes"] += [f"cleanup: {e}" for e in cleanup_errors]
    run["duration_ms"] = round((time.perf_counter() - t0) * 1000, 1)

    if ctl is not None:
        run["events"] = ctl.events
        run["ledger"] = ctl.ledger.records()
        run["attempts"] = ctl.ledger.attempts()
        run["snapshots"] = ctl.snapshots
        run["repeats"] = ctl.repeats
        run["inconclusive"] = ctl.inconclusive
        run["notes"] += ctl.notes
        run["decisions"] = {str(k): v for k, v in ctl.decisions.items()}
        run["statement_log"] = ctl.statement_log  # every executed statement (not part of the trace hash)
        if external:  # what each sandboxed actor wrote to stderr (bounded capture; the rest is counted only)
            run["actor_output"] = {name: {"stderr_bytes": p.child.stderr_bytes, "truncated": p.child.stderr_truncated,
                                          "tail": p.child.stderr_tail[-5:]}
                                   for name, p in ctl.actors.items() if p.child is not None}

    if run["infra_error"]:
        run["invariants"] = infra_results(run["infra_error"])
    else:
        oracle = RunOracle(strategy=info.strategy, initial_orders=schedule["initial_orders"],
                           attempts=run["attempts"], snapshots=run["snapshots"], repeats=run["repeats"],
                           target_stage=scenario.target_stage, inconclusive=run["inconclusive"])
        run["invariants"] = oracle.evaluate()
    run["verdict"] = verdict(run["invariants"], run["infra_error"], run["inconclusive"])
    run["trace_hash"] = canonical_hash(logical_trace(run["events"])) if run["events"] else None
    run["outcome_hash"] = canonical_hash({
        "attempts": [{k: a[k] for k in ("attempt_id", "status", "reason", "result", "error")}
                     for a in run["attempts"]],
        "invariants": [(r["id"], r["status"]) for r in run["invariants"]],
        "final": run["snapshots"][-1]["rows"] if run["snapshots"] else None,
    })
    run["metrics"] = summarize(run)
    return run


def _candidate_admission_error(exc: Exception) -> tuple[str, str] | None:
    """Errors that stop an external candidate before it executes (fail closed)."""
    from gauntlet.candidates.sandbox import IsolationConfigError, IsolationUnavailable
    from gauntlet.candidates.snapshots import CandidateError
    if isinstance(exc, IsolationUnavailable):
        return str(exc), "isolation_unavailable"
    if isinstance(exc, IsolationConfigError):
        return f"invalid isolation configuration: {exc}", "isolation_unavailable"
    if isinstance(exc, CandidateError):
        return str(exc), "candidate_rejected"
    return None


def summarize(run: dict) -> dict:
    atts = run["attempts"]
    ev = run["events"]
    count = lambda pred: sum(1 for x in atts if pred(x))  # noqa: E731
    return {
        "events": len(ev),
        "statements": sum(1 for e in ev if e["kind"] in ("stmt", "unblocked")),
        "attempts": len(atts),
        "acknowledged": count(lambda a: a["status"] == "ok"),
        "failed": count(lambda a: a["status"] == "failed"),
        "uncertain": count(lambda a: a["status"] in ("uncertain", "open")),
        "faults_injected": sum(1 for e in ev if e.get("fault")),
        "crashes": sum(1 for e in ev if e["kind"] == "crash"),
        "lock_waits": sum(1 for e in ev if e["kind"] == "blocked"),
        "recoveries": sum(1 for e in ev if e.get("recovery") or (e["kind"] == "invoke" and e.get("retry"))),
        "snapshots": len(run["snapshots"]),
        "invariants": {s: sum(1 for r in run["invariants"] if r["status"] == s)
                       for s in ("pass", "fail", "inconclusive", "not_applicable", "infra_error")},
    }
