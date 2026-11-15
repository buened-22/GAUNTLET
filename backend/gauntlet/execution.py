"""Shared execution service: execute a (catalog or explicit) schedule, persist it, replay it.

Every feature that executes code under test goes through ``run_and_save`` so all runs land in the
same ``runs`` table with the same record format, oracle and provenance. Each call executes against a
newly created PostgreSQL database (``runner.execute_run``): no state is shared between executions.
"""

from __future__ import annotations

from gauntlet.config import PG_ADMIN_DSN
from gauntlet.engine.runner import execute_run
from gauntlet.store import Store

REPLAY_FIELDS = ("schedule_hash", "trace_hash", "outcome_hash", "verdict")


def run_and_save(store: Store | None, scenario_id: str, impl_id: str, seed: int, *, schedule: dict | None = None,
                 origin: dict | None = None, time_limit_s: float | None = None, max_events: int | None = None,
                 admin_dsn: str = PG_ADMIN_DSN, isolation: dict | None = None) -> dict:
    run = execute_run(scenario_id, impl_id, seed, admin_dsn, schedule=schedule, origin=origin,
                      time_limit_s=time_limit_s, max_events=max_events, isolation=isolation)
    if store is not None:
        store.save_run(run)
    return run


def outcome_class(run: dict) -> str:
    """pass | fail | inconclusive | timeout | limit_exceeded | schedule_error | infrastructure, or another
    ``error_class`` of the run (for external candidates e.g. ``candidate_execution_failure``,
    ``candidate_statement_timeout``)."""
    if run.get("infra_error"):
        return run.get("error_class") or "infrastructure"
    return run["verdict"]


def recorded_isolation(run: dict) -> dict | None:
    """The isolation configuration an external-candidate run was executed with (None for built-ins), so
    replays and minimizations re-execute it under the same limits and acknowledgement."""
    return (run.get("isolation") or {}).get("config")


def replay(store: Store | None, original: dict, *, origin: dict | None = None,
           time_limit_s: float | None = None, admin_dsn: str = PG_ADMIN_DSN) -> dict:
    """Re-execute a persisted run's exact schedule on a fresh database and compare the logical hashes.

    Catalog runs are rebuilt from (scenario, seed); explicit schedules are re-executed from the stored
    schedule document. Physical timing is not compared (it is never part of the hashes).
    """
    explicit = original.get("schedule_source") not in (None, "catalog", "fixture")
    again = run_and_save(store, original["scenario_id"], original["impl_id"], original["seed"],
                         schedule=original["schedule"] if explicit else None,
                         origin=origin or {"kind": "replay", "of": original["id"]},
                         time_limit_s=time_limit_s, admin_dsn=admin_dsn,
                         isolation=recorded_isolation(original))
    same = {h: original.get(h) == again.get(h) for h in REPLAY_FIELDS}
    return {"original_id": original["id"], "replay_id": again["id"], "same": same,
            "reproduced": all(same.values()), "run": again}
