"""A statement that exceeds the step timeout (audit finding FR-3), end to end on PostgreSQL.

The candidates here are SYNTHETIC, trusted fixtures written for these tests (never AI-generated code); process
isolation is not a security boundary. The step timeout is shortened to keep the tests fast.
"""

from __future__ import annotations

import json
import os
import pathlib
import shutil
import sys
import time

import psycopg
import pytest
from psycopg import conninfo, sql

import gauntlet.actor.proxy as proxy
import gauntlet.engine.controller as controller
from gauntlet.candidates.snapshots import register
from gauntlet.config import PG_ADMIN_DSN
from gauntlet.engine.runner import execute_run, schedule_definition
from gauntlet.impls.registry import IMPLEMENTATIONS

pytestmark = pytest.mark.pg

HERE = pathlib.Path(__file__).resolve().parent
GOOD = HERE / "candidates" / "synthetic_ref_a_copy"
INJECT = HERE / "hang_inject"
F01 = "F01_backfill_race_probe"
ISO = {"trusted_environment": True}
STEP = 4.0


@pytest.fixture
def env(tmp_path, monkeypatch, pg_available):
    monkeypatch.setenv("GAUNTLET_CANDIDATES_DIR", str(tmp_path / "snapshots"))
    monkeypatch.setattr(proxy, "STEP_TIMEOUT_S", STEP)
    before = _leftovers()
    yield tmp_path
    assert _leftovers() == before, "a run database or candidate role was left behind"


def _leftovers() -> tuple[list, list]:
    with psycopg.connect(PG_ADMIN_DSN, autocommit=True) as c:
        dbs = sorted(r[0] for r in c.execute("SELECT datname FROM pg_database WHERE datname LIKE 'gauntlet_run_%'"))
        roles = sorted(r[0] for r in c.execute("SELECT rolname FROM pg_roles WHERE rolname LIKE 'gauntlet_cand_%'"))
    return dbs, roles


def slow_candidate(root: pathlib.Path, name: str, body: str) -> str:
    """A copy of the synthetic reference-A candidate whose migrator expand first runs ``body``."""
    d = root / name
    shutil.copytree(GOOD, d)
    src = (d / "candidate.py").read_text(encoding="utf-8")
    src += ("\n\nclass Slow(Implementation):\n    def migrator_expand(self, db, a):\n"
            + "\n".join("        " + line for line in body.strip().splitlines())
            + "\n        return super().migrator_expand(db, a)\n")
    (d / "candidate.py").write_text(src, encoding="utf-8")
    m = json.loads((d / "gauntlet_candidate.json").read_text(encoding="utf-8"))
    m["name"], m["entry"]["class"] = name.replace("_", "-"), "Slow"
    (d / "gauntlet_candidate.json").write_text(json.dumps(m), encoding="utf-8")
    return register(d)["impl_id"]


def step_evidence(run: dict) -> dict:
    notes = [n for n in run["notes"] if n.startswith("step timeout evidence: ")]
    assert len(notes) == 1, run["notes"]
    return json.loads(notes[0].split(": ", 1)[1])


def assert_unjudged(run: dict) -> None:
    assert run["verdict"] == "infra_error" and run["verdict"] != "pass"
    assert all(i["status"] == "infra_error" for i in run["invariants"]), "a timed-out run is never judged"


def test_candidate_statement_over_the_step_timeout_is_a_candidate_statement_timeout(env):
    impl = slow_candidate(env, "sleeper", 'db.execute("SELECT pg_sleep(60)")')
    t = time.monotonic()
    r = execute_run(F01, impl, 1, time_limit_s=120, isolation=ISO)
    took = time.monotonic() - t
    assert r["error_class"] == "candidate_statement_timeout", r["infra_error"]
    assert_unjudged(r)
    ev = step_evidence(r)
    assert ev["cancel"] == "cancelled" and ev["max_loop_gap_s"] <= ev["stall_gap_s"]
    assert ev["activity"]["state"] == "active" and ev["activity"]["wait_event"] == "PgSleep"
    assert ev["activity"]["elapsed_s"] >= 0.9 * STEP
    assert "attributed to the statement" in r["infra_error"]
    assert took < 45, f"the run took {took:.1f}s although the statement was cancelled after {STEP}s"


def test_sqlstate_57014_without_the_servers_view_stays_infrastructure(env, monkeypatch):
    """The cancel still ends the statement with 57014, but the harness could not read the server's view of the
    statement: 57014 alone never attributes the timeout to the candidate."""
    def unanswered(self, pid):
        raise RuntimeError("simulated: the monitor query did not answer")

    monkeypatch.setattr(controller.Controller, "_activity", unanswered)
    impl = slow_candidate(env, "sleeper_noview", 'db.execute("SELECT pg_sleep(60)")')
    r = execute_run(F01, impl, 1, time_limit_s=120, isolation=ISO)
    assert r["error_class"] == "infrastructure", r["infra_error"]
    assert_unjudged(r)
    ev = step_evidence(r)
    assert ev["cancel"] == "cancelled" and ev["activity"] is None and "did not answer" in ev["activity_error"]
    assert "not attributed to the candidate" in r["infra_error"]


def test_harness_stall_during_the_wait_stays_infrastructure(env, monkeypatch):
    """Every lock probe takes 1.5 s (a stalled harness or monitor), longer than the allowed loop gap."""
    real = controller.blocking_pids

    def stalled(conn, pid):
        time.sleep(1.5)
        return real(conn, pid)

    monkeypatch.setattr(controller, "blocking_pids", stalled)
    monkeypatch.setattr(proxy, "STEP_STALL_GAP_S", 1.0)
    impl = slow_candidate(env, "sleeper_stall", 'db.execute("SELECT pg_sleep(60)")')
    r = execute_run(F01, impl, 1, time_limit_s=180, isolation=ISO)
    assert r["error_class"] == "infrastructure", r["infra_error"]
    assert_unjudged(r)
    ev = step_evidence(r)
    assert ev["max_loop_gap_s"] > 1.0 and "stalled" in r["infra_error"]


def test_timed_out_actor_is_shut_down_and_its_locks_and_transaction_are_released(env):
    """The slow statement runs inside a transaction that has updated (and locked) a row. After the run: no session
    of the run remains, the row is not locked, and the update was rolled back."""
    initial = schedule_definition(F01, 1)["initial_orders"]
    oid = min(int(k) for k in initial)
    impl = slow_candidate(env, "locker", f"""
with db.transaction():
    db.execute("UPDATE orders SET total = total + 1 WHERE id = {oid}")
    db.execute("SELECT pg_sleep(60)")
""")
    r = execute_run(F01, impl, 1, time_limit_s=120, isolation=ISO, keep_database=True)
    name, role, observer = r["database"], r["database_role"]["name"], r["database_role"]["observer"]
    try:
        assert r["error_class"] == "candidate_statement_timeout", r["infra_error"]
        assert_unjudged(r)
        with psycopg.connect(PG_ADMIN_DSN, autocommit=True) as c:
            left = c.execute("SELECT pid, application_name, state FROM pg_stat_activity WHERE datname = %s",
                             (name,)).fetchall()
        assert left == [], f"sessions of the run are still open: {left}"
        params = conninfo.conninfo_to_dict(PG_ADMIN_DSN)
        params["dbname"] = name
        with psycopg.connect(conninfo.make_conninfo(**params), autocommit=True) as c:
            c.execute("SET lock_timeout = '2s'")
            total = c.execute("SELECT total::text FROM orders WHERE id = %s", (oid,)).fetchone()[0]
            assert total == initial[str(oid)], "the timed-out transaction was not rolled back"
            assert c.execute("UPDATE orders SET total = total WHERE id = %s", (oid,)).rowcount == 1  # not locked
    finally:
        with psycopg.connect(PG_ADMIN_DSN, autocommit=True) as c:
            c.execute(sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)").format(sql.Identifier(name)))
            # keep_database=True leaves the database and BOTH per-run roles to the caller
            for kept in (role, observer):
                c.execute(sql.SQL("DROP ROLE IF EXISTS {}").format(sql.Identifier(kept)))
            left = c.execute("SELECT rolname FROM pg_roles WHERE rolname = ANY(%s)", ([role, observer],)).fetchall()
            assert left == [], f"per-run roles were left on the server: {left}"


def test_trusted_implementation_timeout_stays_infrastructure(env, monkeypatch):
    """A built-in (trusted) implementation is never 'the candidate': even with complete evidence, its step
    timeout remains an infrastructure error, as before."""
    sys.path.insert(0, str(INJECT))
    try:
        import hang_test_impls
    finally:
        sys.path.remove(str(INJECT))
    monkeypatch.setitem(IMPLEMENTATIONS, hang_test_impls.SLOW_IMPL_ID, hang_test_impls.slow_info())
    monkeypatch.setenv("PYTHONPATH", str(INJECT) + os.pathsep + os.environ.get("PYTHONPATH", ""))
    r = execute_run(F01, hang_test_impls.SLOW_IMPL_ID, 1, time_limit_s=120)
    assert r["error_class"] == "infrastructure", r["infra_error"]
    assert_unjudged(r)
    ev = step_evidence(r)
    assert ev["cancel"] == "cancelled"


def test_run_time_limit_still_takes_precedence_over_the_step_timeout(env):
    """With a run limit shorter than the step timeout, the watchdog ends the run first: class 'timeout'."""
    impl = slow_candidate(env, "sleeper_runlimit", 'db.execute("SELECT pg_sleep(60)")')
    r = execute_run(F01, impl, 1, time_limit_s=3, isolation=ISO)
    assert r["error_class"] == "timeout", r["infra_error"]
    assert_unjudged(r)
