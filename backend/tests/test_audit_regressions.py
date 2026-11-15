"""Regression tests for defects found in the post-implementation audit (no database needed).

Each test reproduces one confirmed defect; see the audit report for root causes.
"""

import copy
from concurrent.futures import ThreadPoolExecutor

import pytest
from fastapi.testclient import TestClient

from gauntlet.api.app import create_app
from gauntlet.differential import compare_pair
from gauntlet.experiment import generate_report, new_experiment
from gauntlet.explore import explore
from gauntlet.minimize import matches, minimize, signature
from gauntlet.schedules import build_schedule
from gauntlet.store import Store

from test_differential import BASE_ATTEMPTS, BASE_ROWS, run as drun
from test_explore import fake_executor_factory, toy_schedule
from test_minimize import factory, make_run, needs


# -- exploration --------------------------------------------------------------------------------------
def test_cancelled_exploration_performs_no_further_executions():
    calls = []
    ex = fake_executor_factory(calls)
    state = {"n": 0}

    def cancel():  # cancel after the 6th schedule (by then the toy failure has been found)
        state["n"] += 1
        return state["n"] > 6
    r = explore(None, "F01_backfill_race_probe", "x", 1, base_schedule=toy_schedule(), executor=ex,
                limits={"max_preemptions": 1}, cancel=cancel)["result"]
    assert r["status"] == "interrupted"
    assert len(calls) == r["schedules_executed"], "no replay executions after cancellation"
    assert all(c.get("replay", {}).get("skipped") for c in r["failure_classes"])


def test_exploration_with_errors_is_not_reported_as_completed():
    calls = []
    r = explore(None, "F01_backfill_race_probe", "x", 1, base_schedule=toy_schedule(),
                executor=fake_executor_factory(calls, infra_on={1, 2}), limits={"max_preemptions": 1, "max_retries": 1},
                replay_failures=False)["result"]
    assert r["status"] == "incomplete" and not r["exhaustive_within_bounds"]


# -- minimization -------------------------------------------------------------------------------------
def _c1_run(schedule, rid, op, code):
    r = make_run(schedule, rid)
    r["invariants"].append({"id": "C1_COMPATIBILITY", "status": "fail", "category": "compatibility",
                            "evidence": [{"actor": "v1-a", "attempt_id": "L050.a1", "op": op, "role": "v1",
                                          "error": {"code": code}, "seq": 9}]})
    r["verdict"] = "fail"
    return r


def test_signature_distinguishes_failures_without_order_evidence():
    s = build_schedule("S02_backfill_vs_legacy_writes", 1)
    target = signature(_c1_run(s, "a", "set_total", "undefined_column"), "C1_COMPATIBILITY")
    same = signature(_c1_run(s, "b", "set_total", "undefined_column"), "C1_COMPATIBILITY")
    other_op = signature(_c1_run(s, "c", "get_order", "undefined_column"), "C1_COMPATIBILITY")
    other_err = signature(_c1_run(s, "d", "set_total", "timeout"), "C1_COMPATIBILITY")
    assert matches(target, same)[0]
    assert not matches(target, other_op)[0], "a C1 failure of a different operation is an unrelated failure"
    assert not matches(target, other_err)[0], "a C1 failure with a different error code is an unrelated failure"


def test_snapshot_row_evidence_is_part_of_the_signature():
    s = build_schedule("S02_backfill_vs_legacy_writes", 1)
    def s3(rid, ids):
        r = make_run(s, rid)
        r["invariants"].append({"id": "S3_REPR_AGREEMENT", "status": "fail", "category": "safety",
                                "evidence": [{"snapshot": "final", "seq": 3, "rows": [{"id": i} for i in ids]}]})
        return r
    t = signature(s3("a", [2, 5]), "S3_REPR_AGREEMENT")
    assert matches(t, signature(s3("b", [5]), "S3_REPR_AGREEMENT"))[0]
    assert not matches(t, signature(s3("c", [9]), "S3_REPR_AGREEMENT"))[0]


def test_minimization_budget_counts_executions_not_attempt_records():
    s = build_schedule("S02_backfill_vs_legacy_writes", 1)
    orig = make_run(s, "orig", fail_inv="S1_ACKED_DURABLE")
    calls = []
    r = minimize(None, "orig", original_run=orig, executor=factory(needs(lids=("L002",)), calls),
                 budget={"max_attempts": 4, "confirm_repeats": 3})["result"]
    candidate_execs = [c for c in calls if c.startswith("ddmin")]
    assert len(candidate_execs) <= 4, f"{len(candidate_execs)} candidate executions for a budget of 4"
    assert r["attempts_used"] == len(candidate_execs)


def test_removing_every_unit_is_reported_minimal():
    s = build_schedule("S02_backfill_vs_legacy_writes", 1)
    orig = make_run(s, "orig", fail_inv="S1_ACKED_DURABLE")
    r = minimize(None, "orig", original_run=orig, executor=factory(lambda sc: True, []),
                 budget={"max_attempts": 500})["result"]
    assert r["units_kept"] == 0 and r["one_minimal"] and r["minimality"].startswith("1-minimal")


def test_cancelled_minimization_performs_no_final_replay():
    s = build_schedule("S02_backfill_vs_legacy_writes", 1)
    orig = make_run(s, "orig", fail_inv="S1_ACKED_DURABLE")
    calls = []
    n = {"i": 0}

    def cancel():
        n["i"] += 1
        return n["i"] > 3
    r = minimize(None, "orig", original_run=orig, executor=factory(needs(lids=("L002",)), calls), cancel=cancel)["result"]
    assert r["status"] == "interrupted" and "final-replay" not in calls
    assert r["minimized"]["replay"] is None and "not replayed" in r["minimized"]["replay_note"]


# -- differential -------------------------------------------------------------------------------------
@pytest.mark.parametrize("field,value", [
    ("not_null", {"id": True, "total_cents": False}),        # constraint lost
    ("triggers", ["orders_sync_total"]),                      # migration left a trigger behind
    ("impl_reported_stage", "finalized"),                     # migration state differs
])
def test_final_constraints_triggers_and_migration_state_are_compared(field, value):
    a = drun("ra", "impl_a", copy.deepcopy(BASE_ATTEMPTS), copy.deepcopy(BASE_ROWS))
    b = drun("rb", "impl_b", copy.deepcopy(BASE_ATTEMPTS), copy.deepcopy(BASE_ROWS))
    for r in (a, b):
        r["snapshots"][-1].update(not_null={"id": True, "total_cents": True}, triggers=[], impl_reported_stage="contracted")
    b["snapshots"][-1][field] = value
    assert compare_pair(a, b)["differs"], field


# -- experiment reporting ----------------------------------------------------------------------------
def _save(st, rid, impl, kind, verdict, version="0.3.0", seed=1, sc="S02_backfill_vs_legacy_writes", bench=None):
    st.save_run({"id": rid, "created_at": "2026-01-01T00:00:00", "benchmark_version": version, "scenario_id": sc,
                 "impl_id": impl, "impl_kind": kind, "seed": seed, "verdict": verdict, "schedule_hash": "s",
                 "trace_hash": rid, "outcome_hash": rid, "duration_ms": 1.0, "events": [],
                 "invariants": [{"id": "S1_ACKED_DURABLE", "status": "fail" if verdict == "fail" else "pass"}]},
                benchmark_id=bench)


def test_exploration_schedules_are_not_counted_as_independent_runs(tmp_path):
    st = Store(tmp_path / "x.sqlite3")
    _save(st, "a", "ref_a_trigger", "reference", "pass")
    for i in range(5):
        _save(st, f"e{i}", "m1_stale_backfill", "mutant", "fail", sc="F01_backfill_race_probe")
    eid = st.create_analysis("exploration", {"impl_id": "m1_stale_backfill", "scenario_id": "F01_backfill_race_probe",
                                             "limits": {"max_preemptions": 1}}, status="completed")
    st.update_analysis(eid, result={"schedules": [{"run_id": f"e{i}"} for i in range(5)], "schedules_executed": 5,
                                    "unique_failure_classes": 1, "exhaustive_within_bounds": True})
    x = new_experiment(st, {"title": "t", "selection": {"run_ids": ["a"], "analysis_ids": [eid]}})
    m = {k["id"]: k for k in generate_report(st, x)["results"]["metrics"]}
    assert m["M2"]["denominator"] == 1, "explored schedules must not inflate the run-level violation rate"
    assert m["M9"]["explorations"][0]["schedules_executed"] == 5


def test_missing_benchmark_runs_are_reported_missing_not_dropped(tmp_path):
    st = Store(tmp_path / "x.sqlite3")
    _save(st, "a", "ref_a_trigger", "reference", "pass", bench="b1")
    st.create_benchmark("b1", "2026-01-01", {"impls": ["ref_a_trigger"], "scenarios": ["S02_backfill_vs_legacy_writes"], "seeds": [1, 2]})
    st.finish_benchmark("b1", "2026-01-02", {"runs": [{"id": "a"}, {"id": "gone"}], "reproducibility": {"checked": 0, "reproduced": 0, "details": []}})
    rep = generate_report(st, new_experiment(st, {"title": "t", "selection": {"benchmark_id": "b1"}}))
    m = {k["id"]: k for k in rep["results"]["metrics"]}
    assert m["M1"]["counts"]["missing"] == 1 and m["M1"]["ids"]["missing"] == ["gone"]
    assert rep["results"]["completeness"]["status"] == "incomplete"


def test_mixed_benchmark_versions_are_not_silently_combined(tmp_path):
    st = Store(tmp_path / "x.sqlite3")
    _save(st, "a", "ref_a_trigger", "reference", "pass", version="0.2.0")
    _save(st, "b", "ref_a_trigger", "reference", "pass", version="0.3.0", seed=2)
    x = new_experiment(st, {"title": "t", "selection": {"run_ids": ["a", "b"]}})
    with pytest.raises(ValueError, match="benchmark versions"):
        generate_report(st, x)
    y = new_experiment(st, {"title": "t", "selection": {"run_ids": ["a", "b"]}, "allow_mixed_versions": True})
    assert "mixed benchmark versions" in generate_report(st, y)["markdown"]


def test_modified_or_missing_evidence_is_detected(tmp_path):
    from gauntlet.experiment import verify_manifest
    st = Store(tmp_path / "x.sqlite3")
    _save(st, "a", "ref_a_trigger", "reference", "pass")
    _save(st, "b", "m1_stale_backfill", "mutant", "fail")
    rep = generate_report(st, new_experiment(st, {"title": "t", "selection": {"run_ids": ["a", "b"]}}))
    assert verify_manifest(st, rep["manifest"])["ok"]
    with st._conn() as c:
        c.execute("UPDATE runs SET data_json = replace(data_json, '\"fail\"', '\"pass\"') WHERE id = 'b'")
        c.execute("DELETE FROM runs WHERE id = 'a'")
    v = verify_manifest(st, rep["manifest"])
    assert not v["ok"] and v["modified"] == ["b"] and v["missing"] == ["a"]


# -- API ---------------------------------------------------------------------------------------------
@pytest.fixture
def client(tmp_path):
    st = Store(tmp_path / "api.sqlite3")
    with TestClient(create_app(store=st, executor=ThreadPoolExecutor(max_workers=1),
                               jobs=ThreadPoolExecutor(max_workers=1)), base_url="http://127.0.0.1:8000") as c:
        c.store = st
        yield c


def test_cancel_is_refused_for_jobs_that_cannot_be_cancelled(client):
    for kind in ("differential", "replay"):
        aid = client.store.create_analysis(kind, {}, status="running")
        r = client.post(f"/api/analyses/{aid}/cancel")
        assert r.status_code == 409 and "cannot be cancelled" in r.json()["detail"]
    aid = client.store.create_analysis("exploration", {}, status="running")
    assert client.post(f"/api/analyses/{aid}/cancel").status_code == 200


def test_partially_captured_run_makes_the_comparison_inconclusive():
    from gauntlet.differential import classify
    a = drun("ra", "impl_a", copy.deepcopy(BASE_ATTEMPTS), copy.deepcopy(BASE_ROWS))
    b = {"id": "rb", "impl_id": "impl_b", "infra_error": None, "attempts": copy.deepcopy(BASE_ATTEMPTS),
         "snapshots": copy.deepcopy(a["snapshots"])}  # no verdict, no invariants
    c = classify([a, b], [compare_pair(a, b)])
    assert c["classification"] == "inconclusive" and "incomplete run record" in c["reason"]


def test_failure_class_replay_error_is_not_called_flaky():
    calls = []
    base = fake_executor_factory(calls)

    def ex(store, *a, **k):
        r = base(store, *a, **k)
        if k["origin"]["kind"] == "exploration-replay":
            r.update(infra_error="PostgreSQL unreachable", error_class="infrastructure", verdict="infra_error")
        return r
    r = explore(None, "F01_backfill_race_probe", "x", 1, base_schedule=toy_schedule(), executor=ex,
                limits={"max_preemptions": 1})["result"]
    c = r["failure_classes"][0]
    assert c["replay"]["reproduced"] is None and not c.get("flaky") and "not established" in c["replay"]["note"]


def test_nondeterministic_candidate_is_rejected_when_confirmation_is_required():
    s = build_schedule("S02_backfill_vs_legacy_writes", 1)
    orig = make_run(s, "orig", fail_inv="S1_ACKED_DURABLE")
    calls = []
    base = factory(needs(lids=("L002",)), calls)

    def ex(store, scenario_id, impl_id, seed, *, schedule, origin, time_limit_s, max_events):
        r = base(store, scenario_id, impl_id, seed, schedule=schedule, origin=origin, time_limit_s=time_limit_s,
                 max_events=max_events)
        # reduced candidates reproduce only on every other execution
        if origin["step"].startswith("ddmin") and len(calls) % 2 == 0:
            return make_run(schedule, r["id"])
        return r
    res = minimize(None, "orig", original_run=orig, executor=ex, budget={"max_attempts": 200, "confirm_repeats": 2})["result"]
    assert res["units_kept"] == res["units_total"], "a candidate that did not reproduce every time must not be accepted"
    assert all(a["result"] != "accepted" for a in res["attempts"])


def test_regenerated_report_and_api_detect_changed_evidence(client):
    st = client.store
    _save(st, "a", "ref_a_trigger", "reference", "pass")
    _save(st, "b", "m1_stale_backfill", "mutant", "fail")
    eid = client.post("/api/experiments", json={"config": {"title": "t", "selection": {"run_ids": ["a", "b"]}}}).json()["id"]
    assert client.get(f"/api/experiments/{eid}/verify").status_code == 409  # nothing reported yet
    first = client.post(f"/api/experiments/{eid}/report").json()
    assert "evidence_check_against_previous_report" not in first
    assert client.get(f"/api/experiments/{eid}/verify").json()["ok"]
    with st._conn() as c:
        c.execute("UPDATE runs SET data_json = replace(data_json, '\"fail\"', '\"pass\"') WHERE id = 'b'")
    v = client.get(f"/api/experiments/{eid}/verify").json()
    assert not v["ok"] and v["modified"] == ["b"]
    second = client.post(f"/api/experiments/{eid}/report").json()
    assert second["evidence_check_against_previous_report"]["modified"] == ["b"]


def test_nondeterministic_executions_are_not_expanded_twice_and_block_exhaustiveness():
    calls = []
    base = fake_executor_factory(calls)

    def ignores_prefix(store, scenario_id, impl_id, seed, *, schedule, origin, time_limit_s, max_events):
        s = copy.deepcopy(schedule)
        for d in s["directives"]:
            if d.get("explore"):
                d["choices"] = []  # always realizes the default schedule, whatever prefix was asked for
        return base(store, scenario_id, impl_id, seed, schedule=s, origin=origin, time_limit_s=time_limit_s,
                    max_events=max_events)
    r = explore(None, "F01_backfill_race_probe", "x", 1, base_schedule=toy_schedule(), executor=ignores_prefix,
                limits={"max_preemptions": 3, "max_schedules": 1000}, replay_failures=False)["result"]
    assert r["counters"]["duplicates"] > 0 and r["schedules_executed"] < 50
    assert r["status"] == "incomplete" and not r["exhaustive_within_bounds"]
