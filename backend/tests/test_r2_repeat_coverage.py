"""R2 coverage regression (v0.3.0): S04 repeats `expand` after the backfill has populated total_cents.

Uses a test-only implementation (tests/r2_inject/) that destroys total_cents on a repeated expand.
"""

import copy
import os
import pathlib
import sys

import psycopg
import pytest

from gauntlet.actor.proxy import ProcessPool
from gauntlet.engine.controller import Controller
from gauntlet.engine.runner import execute_run, schedule_definition
from gauntlet.impls.registry import get_info
from gauntlet.oracle.checker import RunOracle, verdict
from gauntlet.pg.provision import apply_legacy_schema, create_run_database, drop_run_database
from gauntlet.scenarios.catalog import get_scenario

pytestmark = pytest.mark.pg

INJECT = pathlib.Path(__file__).resolve().parent / "r2_inject"
sys.path.insert(0, str(INJECT))
import r2_test_impls  # noqa: E402

r2_test_impls.register()
ERASER = r2_test_impls.TEST_IMPL_ID
S04 = "S04_crash_and_resume"


@pytest.fixture
def inject_env(monkeypatch, pg_available):
    """Make the test-only implementation loadable inside actor subprocesses."""
    monkeypatch.setenv("PYTHONPATH", str(INJECT) + os.pathsep + os.environ.get("PYTHONPATH", ""))


def expand_lid(sched):
    return next(d["logical_id"] for d in sched["directives"]
                if d["kind"] == "call" and sched["ops"][d["logical_id"]]["op"] == "expand")


def v020_s04_schedule(seed):
    """The v0.3.0 S04 schedule minus the single post-backfill expand repeat (= the v0.2.0 shape)."""
    sched = copy.deepcopy(schedule_definition(S04, seed))
    ds, ex = sched["directives"], expand_lid(sched)
    at = next(i for i, d in enumerate(ds) if d["kind"] == "snapshot" and d.get("label") == "backfilled")
    assert ds[at + 1] == {"kind": "repeat", "actor": "migrator", "logical_id": ex, "fault": "F5"}
    del ds[at + 1]
    return sched


def run_schedule(impl_id, sched):
    """Same engine and oracle steps as execute_run, with an explicit schedule."""
    info = get_info(impl_id)
    db = create_run_database(prefix="gauntlet_test")
    try:
        apply_legacy_schema(db.dsn, {int(k): v for k, v in sched["initial_orders"].items()})
        pool = ProcessPool(sum(1 for d in sched["directives"] if d["kind"] in ("spawn", "restart")))
        with psycopg.connect(db.dsn, autocommit=True) as mon:
            ctl = Controller(impl_id, db.dsn, mon, copy.deepcopy(sched["ops"]), pool)
            try:
                ctl.execute(sched["directives"])
                ctl.settle()
            finally:
                ctl.shutdown()
            res = RunOracle(strategy=info.strategy, initial_orders=sched["initial_orders"],
                            attempts=ctl.ledger.attempts(), snapshots=ctl.snapshots, repeats=ctl.repeats,
                            target_stage=get_scenario(S04).target_stage, inconclusive=ctl.inconclusive).evaluate()
        return {"invariants": res, "verdict": verdict(res, None, ctl.inconclusive), "snapshots": ctl.snapshots,
                "repeats": ctl.repeats}
    finally:
        drop_run_database(db)


def inv(run, inv_id):
    return next(i for i in run["invariants"] if i["id"] == inv_id)


@pytest.mark.parametrize("impl", ["ref_a_trigger", "ref_b_dualwrite"])
@pytest.mark.parametrize("seed", [1, 2, 3])
def test_references_pass_s04_with_post_backfill_repeat(impl, seed, pg_available):
    run = execute_run(S04, impl, seed)
    assert run["infra_error"] is None and run["verdict"] == "pass", \
        [(i["id"], i["status"], i["summary"]) for i in run["invariants"] if i["status"] != "pass"]
    r2 = inv(run, "R2_REPEAT_SAFE")
    assert r2["status"] == "pass" and len(r2["evidence"]) == 5
    ex = expand_lid(run["schedule"])
    assert [e["changed"] for e in r2["evidence"] if e["logical_id"] == ex] == [[], []]


def test_erasing_repeat_fails_r2_with_changed_values(inject_env):
    run = execute_run(S04, ERASER, 1)
    assert run["infra_error"] is None
    r2 = inv(run, "R2_REPEAT_SAFE")
    assert r2["status"] == "fail"
    (entry,) = r2["evidence"]  # exactly one repeated step changed state
    assert entry["op"] == "expand" and entry["logical_id"] == expand_lid(run["schedule"])
    assert entry["status"] == "ok" and entry["changed"] == ["rows"]
    backfilled = next(s for s in run["snapshots"] if s["label"] == "backfilled")
    assert entry["pre_seq"] > backfilled["seq"] and r2["trigger_seq"] == entry["post_seq"]
    # The changed values, taken from the two snapshots the evidence points to.
    pre = next(s for s in run["snapshots"] if s["seq"] == entry["pre_seq"])
    post = next(s for s in run["snapshots"] if s["seq"] == entry["post_seq"])
    assert pre["rows"] and all(r["total_cents"] is not None for r in pre["rows"])
    assert all(r["total_cents"] is None for r in post["rows"])
    assert [(r["id"], r["total"]) for r in pre["rows"]] == [(r["id"], r["total"]) for r in post["rows"]]


def test_v020_s04_schedule_would_let_the_eraser_pass(inject_env):
    """Without the post-backfill repeat, the same implementation passes S04 entirely: the regression
    the new directive protects against."""
    old = run_schedule(ERASER, v020_s04_schedule(1))
    assert old["verdict"] == "pass"
    assert inv(old, "R2_REPEAT_SAFE")["status"] == "pass"
    new = run_schedule(ERASER, schedule_definition(S04, 1))
    assert inv(new, "R2_REPEAT_SAFE")["status"] == "fail"


def test_eraser_is_not_in_the_benchmark_registry():
    import subprocess
    out = subprocess.run([sys.executable, "-c", "from gauntlet.impls.registry import IMPLEMENTATIONS as I; "
                          "print(len(I), any(k.startswith('test_') for k in I))"],
                         capture_output=True, text=True, env={k: v for k, v in os.environ.items() if k != "PYTHONPATH"},
                         cwd=str(pathlib.Path(__file__).resolve().parents[1]))
    assert out.stdout.split() == ["10", "False"], out.stdout + out.stderr
