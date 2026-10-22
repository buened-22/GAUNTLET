"""PostgreSQL 16 integration tests of the engine, references, mutants and faults."""

import copy

import psycopg
import pytest

from gauntlet.actor.proxy import ActorState, ProcessPool
from gauntlet.engine.controller import Controller
from gauntlet.engine.runner import execute_run
from gauntlet.pg.provision import apply_legacy_schema, create_run_database, drop_run_database

pytestmark = pytest.mark.pg


def status(run, inv):
    return next(r["status"] for r in run["invariants"] if r["id"] == inv)


def test_server_is_postgres_16(pg_available):
    assert pg_available["is_pg16"], pg_available


@pytest.mark.parametrize("impl", ["ref_a_trigger", "ref_b_dualwrite"])
def test_references_pass_baseline(run_cache, impl):
    run = run_cache("S01_baseline_migration", impl)
    assert run["verdict"] == "pass", [(r["id"], r["status"], r["summary"]) for r in run["invariants"]]
    assert status(run, "R4_TERMINAL_STATE") == "pass"
    assert run["metrics"]["statements"] > 50


def test_reference_a_survives_crash_and_resume(run_cache):
    run = run_cache("S04_crash_and_resume", "ref_a_trigger")
    assert run["verdict"] == "pass", [(r["id"], r["status"]) for r in run["invariants"]]
    for inv in ("R1_INTERRUPTED_RESUMES", "R2_REPEAT_SAFE", "R3_BACKFILL_RETRY_SAFE", "R4_TERMINAL_STATE"):
        assert status(run, inv) == "pass", inv
    crashes = [e for e in run["events"] if e["kind"] == "crash"]
    assert len(crashes) == 4
    # Crashed attempts are recorded as uncertain, never as acknowledged.
    crashed = [a for a in run["attempts"] if a["reason"] == "crashed"]
    assert crashed and all(a["status"] == "uncertain" for a in crashed)


def test_stale_backfill_mutant_killed_by_intended_invariant(run_cache):
    run = run_cache("S02_backfill_vs_legacy_writes", "m1_stale_backfill")
    assert run["verdict"] == "fail" and run["infra_error"] is None
    assert status(run, "S1_ACKED_DURABLE") == "fail"
    assert run_cache("S02_backfill_vs_legacy_writes", "ref_a_trigger")["verdict"] == "pass"
    # Evidence names the overwritten order and the observation that exposed it.
    ev = next(r for r in run["invariants"] if r["id"] == "S1_ACKED_DURABLE")["evidence"]
    assert ev and ev[0]["first_conflict"]["seq"] is not None


def test_nonatomic_dual_write_mutant_killed_by_intended_invariant(run_cache):
    run = run_cache("S03_lost_ack_and_retry", "m2_nonatomic_dual_write")
    assert run["verdict"] == "fail" and run["infra_error"] is None
    assert status(run, "S3_REPR_AGREEMENT") == "fail"
    assert run_cache("S03_lost_ack_and_retry", "ref_b_dualwrite")["verdict"] == "pass"


def test_retry_duplication_mutant_diagnosed_precisely(run_cache):
    run = run_cache("S03_lost_ack_and_retry", "m3_retry_duplicates")
    assert status(run, "S4_NO_DUPLICATION") == "fail"
    assert status(run, "S2_EXACT_CENTS") != "fail"


def test_same_seed_reproduces_schedule_trace_and_outcome(run_cache):
    a = run_cache("S02_backfill_vs_legacy_writes", "m1_stale_backfill")
    b = execute_run("S02_backfill_vs_legacy_writes", "m1_stale_backfill", 1)
    assert a["schedule_hash"] == b["schedule_hash"]
    assert a["trace_hash"] == b["trace_hash"]
    assert a["outcome_hash"] == b["outcome_hash"]
    assert a["verdict"] == b["verdict"]


def test_unreachable_database_is_infra_error_not_pass():
    run = execute_run("S01_baseline_migration", "ref_a_trigger", 1,
                      admin_dsn="postgresql://nobody:x@127.0.0.1:1/postgres")
    assert run["verdict"] == "infra_error"
    assert all(r["status"] == "infra_error" for r in run["invariants"])


# --- controller-level fault injection -------------------------------------------

@pytest.fixture
def session(pg_available):
    db = create_run_database(prefix="gauntlet_test")
    apply_legacy_schema(db.dsn, {1: "10.00", 2: "20.00", 3: "30.00"})
    mon = psycopg.connect(db.dsn, autocommit=True)
    ctls = []

    def make(impl="ref_a_trigger", ops=None):
        ctl = Controller(impl, db.dsn, mon, copy.deepcopy(ops or {}), ProcessPool(2))
        ctls.append(ctl)
        return ctl

    yield make, mon
    for c in ctls:
        c.shutdown()
    mon.close()
    drop_run_database(db)


def test_pause_before_write_and_lock_wait_detection(session):
    make, mon = session
    ops = {
        "L1": {"op": "set_total", "args": {"order_id": 1, "total": "11.00", "key": "k1"}, "role": "v1"},
        "L2": {"op": "set_total", "args": {"order_id": 1, "total": "12.00", "key": "k2"}, "role": "v1"},
    }
    ctl = make(ops=ops)
    ctl.execute([{"kind": "spawn", "actor": "a", "role": "v1"}, {"kind": "spawn", "actor": "b", "role": "v1"},
                 {"kind": "call", "actor": "a", "logical_id": "L1"},
                 {"kind": "run_until", "actor": "a", "cond": "before_commit", "n": 1},
                 {"kind": "call", "actor": "b", "logical_id": "L2"},
                 {"kind": "run", "actor": "b"}])
    # b must be blocked on a's uncommitted row lock, detected via pg_blocking_pids.
    assert ctl.actors["b"].state == ActorState.BLOCKED
    assert any(e["kind"] == "blocked" and e["actor"] == "b" for e in ctl.events)
    ctl.execute([{"kind": "run", "actor": "a"}])
    assert any(e["kind"] == "unblocked" and e["actor"] == "b" for e in ctl.events)
    ctl.execute([{"kind": "run", "actor": "b"}])
    assert mon.execute("SELECT total::text FROM orders WHERE id = 1").fetchone()[0] == "12.00"


def test_crash_before_commit_rolls_back_and_after_commit_persists(session):
    make, mon = session
    ops = {
        "L1": {"op": "set_total", "args": {"order_id": 2, "total": "21.00", "key": "k1"}, "role": "v1"},
        "L2": {"op": "set_total", "args": {"order_id": 3, "total": "31.00", "key": "k2"}, "role": "v1"},
    }
    ctl = make(ops=ops)
    ctl.execute([{"kind": "spawn", "actor": "a", "role": "v1"},
                 {"kind": "call", "actor": "a", "logical_id": "L1"},
                 {"kind": "run_until", "actor": "a", "cond": "before_commit", "n": 1},
                 {"kind": "crash", "actor": "a", "fault": "F7"},
                 {"kind": "restart", "actor": "a"},
                 {"kind": "call", "actor": "a", "logical_id": "L2"},
                 {"kind": "run_until", "actor": "a", "cond": "after_commit", "n": 1},
                 {"kind": "crash", "actor": "a", "fault": "F1"}])
    rows = dict(mon.execute("SELECT id, total::text FROM orders ORDER BY id").fetchall())
    assert rows[2] == "20.00"  # killed before COMMIT: rolled back by PostgreSQL
    assert rows[3] == "31.00"  # killed after COMMIT: durable even though the client never heard back
    statuses = {a["attempt_id"]: (a["status"], a["reason"]) for a in ctl.ledger.attempts()}
    assert statuses["L1.a1"] == ("uncertain", "crashed")
    assert statuses["L2.a1"] == ("uncertain", "crashed")
    assert ctl.actors["a"].incarnation == 2


def test_lost_response_then_idempotent_retry(session):
    make, mon = session
    ops = {"L1": {"op": "add_charge", "args": {"order_id": 1, "amount": "0.29", "key": "kx"}, "role": "v1"}}
    ctl = make(ops=ops)
    ctl.execute([{"kind": "spawn", "actor": "a", "role": "v1"},
                 {"kind": "call", "actor": "a", "logical_id": "L1"},
                 {"kind": "lose_response", "actor": "a", "fault": "F1"},
                 {"kind": "call", "actor": "a", "logical_id": "L1", "retry": True},
                 {"kind": "run", "actor": "a"}])
    att = ctl.ledger.attempts()
    assert [a["status"] for a in att] == ["uncertain", "ok"]
    assert att[1]["result"] == {"total": "10.29"}
    assert mon.execute("SELECT total::text FROM orders WHERE id = 1").fetchone()[0] == "10.29"


# --- Regression (validation audit, Finding 1): same idempotency key under a new internal operation id ----

def _key_reuse_run(impl):
    """Sequential, fault-free: two v2 charges with the same key but different logical ids, then a read."""
    from gauntlet.impls.registry import get_info
    from gauntlet.oracle.checker import RunOracle

    initial = {1: "100.00"}
    db = create_run_database(prefix="gauntlet_test")
    try:
        apply_legacy_schema(db.dsn, initial)
        ops = {
            "X1": {"op": "expand", "args": {}, "role": "migrator"},
            "Q1": {"op": "add_charge", "args": {"order_id": 1, "amount": "6.72", "key": "dup"}, "role": "v2"},
            "Q2": {"op": "add_charge", "args": {"order_id": 1, "amount": "6.72", "key": "dup"}, "role": "v2"},
            "Q3": {"op": "get_order", "args": {"order_id": 1}, "role": "v2"},
        }
        directives = [{"kind": "spawn", "actor": "m", "role": "migrator"},
                      {"kind": "call", "actor": "m", "logical_id": "X1"}, {"kind": "run", "actor": "m"},
                      {"kind": "spawn", "actor": "v2", "role": "v2"}]
        for lid in ("Q1", "Q2", "Q3"):
            directives += [{"kind": "call", "actor": "v2", "logical_id": lid}, {"kind": "run", "actor": "v2"}]
        directives.append({"kind": "snapshot", "label": "after"})
        with psycopg.connect(db.dsn, autocommit=True) as mon:
            ctl = Controller(impl, db.dsn, mon, ops, ProcessPool(2))
            try:
                ctl.execute(directives)
                ctl.settle()
            finally:
                ctl.shutdown()
            res = RunOracle(strategy=get_info(impl).strategy, initial_orders=initial, attempts=ctl.ledger.attempts(),
                            snapshots=ctl.snapshots, repeats=[], target_stage="none").evaluate()
        return {r["id"]: r["status"] for r in res}
    finally:
        drop_run_database(db)


def test_key_reuse_under_new_id_reference_passes_and_duplicating_mutant_fails(pg_available):
    ref = _key_reuse_run("ref_a_trigger")
    mut = _key_reuse_run("m3_retry_duplicates")
    assert ref["S7_LINEARIZABLE"] == "pass" and ref["S4_NO_DUPLICATION"] == "pass", ref
    assert mut["S7_LINEARIZABLE"] == "fail" and mut["S4_NO_DUPLICATION"] == "fail", mut
