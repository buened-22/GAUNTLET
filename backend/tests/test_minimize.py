"""Failure minimization: units, candidates, ddmin, failure identity, budgets (fake executor, no database)."""

import copy

import pytest

from gauntlet.minimize import build_candidate, extract_units, matches, minimize, signature
from gauntlet.schedules import build_schedule, schedule_id, validate_schedule
from gauntlet.store import Store

S02 = "S02_backfill_vs_legacy_writes"
S04 = "S04_crash_and_resume"


def ops_in(schedule):
    lids = {d["logical_id"] for d in schedule["directives"] if d.get("logical_id")}
    lids |= {l for d in schedule["directives"] if d["kind"] == "interleave" for q in d["queues"].values() for l in q}
    return lids


def snapshots_in(schedule):
    return {d["label"] for d in schedule["directives"] if d["kind"] == "snapshot"}


def make_run(schedule, rid, *, fail_inv=None, orders=(2,), cats=("lost_ack",), infra=None, error_class=None):
    invs = [{"id": "S1_ACKED_DURABLE", "status": "pass", "category": "safety", "evidence": []},
            {"id": "S5_RECORDS_PRESERVED", "status": "pass", "category": "safety", "evidence": []}]
    for i in invs:
        if i["id"] == fail_inv:
            i["status"] = "fail"
            i["evidence"] = [{"order_id": o, "explained_by": list(cats)} for o in orders]
    n = len(schedule["directives"])
    return {"id": rid, "status": "completed", "scenario_id": schedule["scenario_id"], "impl_id": "m1_stale_backfill",
            "seed": schedule["seed"], "schedule": schedule, "verdict": "infra_error" if infra else ("fail" if fail_inv else "pass"),
            "infra_error": infra, "error_class": error_class, "invariants": invs if not infra else [],
            "attempts": [{"attempt_id": f"A{i}"} for i in range(len(ops_in(schedule)))],
            "events": [{"seq": i} for i in range(3 * n)], "decisions": {},
            "schedule_hash": schedule_id(schedule), "trace_hash": schedule_id(schedule),
            "outcome_hash": schedule_id(schedule)[::-1]}


def needs(lids=(), snaps=()):
    """Failure predicate: fails S1 iff all the given logical ids and snapshot labels are present."""
    return lambda s: set(lids) <= ops_in(s) and set(snaps) <= snapshots_in(s)


def factory(pred, calls, *, other_failure=None, flaky=None, timeout_when=None):
    def ex(store, scenario_id, impl_id, seed, *, schedule, origin, time_limit_s, max_events):
        calls.append(origin["step"])
        rid = f"r{len(calls)}"
        if timeout_when and timeout_when(schedule):
            return make_run(schedule, rid, infra="run exceeded its wall-clock limit", error_class="timeout")
        if flaky and not flaky(len(calls)):
            return make_run(schedule, rid)
        if pred(schedule):
            return make_run(schedule, rid, fail_inv="S1_ACKED_DURABLE")
        if other_failure and other_failure(schedule):
            return make_run(schedule, rid, fail_inv="S5_RECORDS_PRESERVED")
        return make_run(schedule, rid)
    return ex


@pytest.fixture
def original():
    s = build_schedule(S02, 1)
    return make_run(s, "orig", fail_inv="S1_ACKED_DURABLE", orders=(2, 4))


def test_units_cover_every_non_structural_directive():
    for sid in (S02, S04):
        s = build_schedule(sid, 1)
        units = extract_units(s)
        covered = {i for u in units for i in u["directives"]}
        for i, d in enumerate(s["directives"]):
            if d["kind"] in ("call", "run", "run_until", "lose_response", "retry_until_ok", "repeat", "snapshot",
                             "backfill_until_done"):
                assert i in covered, (sid, i, d)


def test_full_candidate_reproduces_original_directives():
    s = build_schedule(S02, 1)
    units = extract_units(s)
    c = build_candidate(s, {}, units, [u["id"] for u in units], derived_from={"kind": "t"})
    assert c["directives"] == s["directives"] and c["ops"] == s["ops"] and not validate_schedule(c)


def test_dependencies_are_preserved_repeat_removed_with_its_operation():
    s = build_schedule(S04, 1)
    units = extract_units(s)
    rep = next(u for u in units if u["kind"] == "repeat")
    dep = rep["deps"][0]
    kept = [u["id"] for u in units if u["id"] != dep]  # remove the operation, keep its repeat "requested"
    c = build_candidate(s, {}, units, kept, derived_from={"kind": "t"})
    lid = dep.split(":", 1)[1]
    assert not any(d.get("logical_id") == lid for d in c["directives"]), "repeat of a removed operation must go too"
    assert not validate_schedule(c)


def test_every_single_unit_removal_is_structurally_valid():
    for sid in (S02, S04):
        s = build_schedule(sid, 1)
        units = extract_units(s)
        for u in units:
            c = build_candidate(s, {}, units, [x["id"] for x in units if x["id"] != u["id"]], derived_from={"kind": "t"})
            assert not validate_schedule(c), (sid, u["id"], validate_schedule(c))


def test_signature_matching_rules():
    s = build_schedule(S02, 1)
    t = signature(make_run(s, "a", fail_inv="S1_ACKED_DURABLE", orders=(2, 4)), "S1_ACKED_DURABLE")
    assert matches(t, signature(make_run(s, "b", fail_inv="S1_ACKED_DURABLE", orders=(4,)), "S1_ACKED_DURABLE"))[0]
    assert not matches(t, signature(make_run(s, "c", fail_inv="S1_ACKED_DURABLE", orders=(9,)), "S1_ACKED_DURABLE"))[0]
    assert matches(t, signature(make_run(s, "c", fail_inv="S1_ACKED_DURABLE", orders=(9,)), "S1_ACKED_DURABLE"), "any")[0]
    assert not matches(t, signature(make_run(s, "d", fail_inv="S1_ACKED_DURABLE", cats=("dup",)), "S1_ACKED_DURABLE"))[0]
    assert not matches(t, signature(make_run(s, "e", fail_inv="S5_RECORDS_PRESERVED"), "S1_ACKED_DURABLE"))[0]
    assert signature(make_run(s, "f", infra="x"), "S1_ACKED_DURABLE") is None


def test_reduces_to_the_failure_inducing_units_and_is_one_minimal(original):
    calls = []
    pred = needs(lids=("L002", "L003"), snaps=("after_scripted_races",))
    r = minimize(None, "orig", original_run=original, executor=factory(pred, calls),
                 budget={"max_attempts": 500})["result"]
    assert r["status"] == "completed" and r["one_minimal"] and r["minimality"].startswith("1-minimal")
    m = r["minimized"]["schedule"]
    assert pred(m) and r["minimized"]["replay"]["signature_match"]
    kept = {u["id"] for u in r["retained_units"]}
    assert kept == {"op:L002", "op:L003", "snapshot:20"} or ({"op:L002", "op:L003"} <= kept and len(kept) == 3), kept
    assert r["reduction"]["directives"][1] < r["reduction"]["directives"][0]
    assert calls[0] == "reproduce-original" and calls[-1] == "final-replay"


def test_unrelated_failures_are_rejected(original):
    calls = []
    pred = needs(lids=("L002", "L003"), snaps=("after_scripted_races",))
    other = lambda s: "after_scripted_races" not in snapshots_in(s)  # noqa: E731  removing the snapshot -> other failure
    r = minimize(None, "orig", original_run=original, executor=factory(pred, calls, other_failure=other),
                 budget={"max_attempts": 500})["result"]
    rejected = [a for a in r["attempts"] if a["result"] == "rejected" and "S5_RECORDS_PRESERVED" in a.get("failed_invariants", [])]
    assert rejected and all("does not FAIL" in a["reason"] for a in rejected)
    assert "after_scripted_races" in snapshots_in(r["minimized"]["schedule"])


def test_flaky_original_is_not_minimized(original):
    calls = []
    pred = needs(lids=("L002",))
    r = minimize(None, "orig", original_run=original, executor=factory(pred, calls, flaky=lambda n: n % 2 == 1),
                 budget={"reproduce_repeats": 2})["result"]
    assert r["status"] == "flaky" and "minimized" not in r and len(calls) == 2


def test_not_reproducible_original(original):
    r = minimize(None, "orig", original_run=original, executor=factory(lambda s: False, []))["result"]
    assert r["status"] == "not_reproducible" and "minimized" not in r


def test_budget_exhaustion_is_reported_and_not_claimed_minimal(original):
    calls = []
    r = minimize(None, "orig", original_run=original, executor=factory(needs(lids=("L002",)), calls),
                 budget={"max_attempts": 3})["result"]
    assert r["status"] == "budget_exhausted" and r["budget_exhausted"] and not r.get("one_minimal")
    assert r["attempts_used"] == 3 and r["minimality"].startswith("not proven")
    assert r["minimized"]["replay"]["signature_match"]


def test_timeouts_are_rejected_never_accepted(original):
    calls = []
    pred = needs(lids=("L002",))
    small = lambda s: len(s["directives"]) < 60  # noqa: E731  every strongly reduced candidate "times out"
    r = minimize(None, "orig", original_run=original, executor=factory(pred, calls, timeout_when=small),
                 budget={"max_attempts": 400})["result"]
    to = [a for a in r["attempts"] if a.get("outcome") == "timeout"]
    assert to and all(a["result"] == "rejected" for a in to)
    assert len(r["minimized"]["schedule"]["directives"]) >= 60


def test_no_reduction_possible(original):
    full = copy.deepcopy(original["schedule"])
    pred = lambda s: s["directives"] == full["directives"]  # noqa: E731  only the exact original fails
    r = minimize(None, "orig", original_run=original, executor=factory(pred, []), budget={"max_attempts": 500})["result"]
    assert r["status"] == "completed" and r["units_kept"] == r["units_total"] and r["one_minimal"]
    assert r["note"].startswith("no reduction")


def test_refuses_runs_without_a_confirmed_violation(original):
    passing = make_run(original["schedule"], "p")
    with pytest.raises(ValueError):
        minimize(None, "p", original_run=passing, executor=factory(lambda s: True, []))
    with pytest.raises(ValueError):
        minimize(None, "orig", original_run=original, invariant="S5_RECORDS_PRESERVED", executor=factory(lambda s: True, []))


def test_original_run_record_is_never_modified(tmp_path, original):
    st = Store(tmp_path / "r.sqlite3")
    original["benchmark_version"], original["created_at"], original["impl_kind"] = "0.3.0", "2026-01-01T00:00:00", "mutant"
    original["duration_ms"] = 1.0
    st.save_run(original)
    before = st.run_record_sha256("orig")
    res = minimize(st, "orig", executor=factory(needs(lids=("L002",)), []), budget={"max_attempts": 20})
    assert st.run_record_sha256("orig") == before and res["result"]["original_unchanged"]
    stored = st.get_analysis(res["id"])
    assert stored["kind"] == "minimization" and stored["result"]["minimized"]["schedule"]


def test_unjudged_single_unit_removals_void_the_one_minimal_claim(original):
    # Every candidate below 60 directives "times out": the final pass's single-unit removals are never judged,
    # so the reduction must not be reported as 1-minimal.
    pred = needs(lids=("L002",))
    small = lambda s: len(s["directives"]) < 60  # noqa: E731
    r = minimize(None, "orig", original_run=original, executor=factory(pred, [], timeout_when=small),
                 budget={"max_attempts": 400})["result"]
    assert r["status"] == "completed" and not r["one_minimal"]
    assert r["minimality"].startswith("not proven") and r["one_minimal_unproven_units"]
    assert "never judged" in r["note"]


def test_judged_rejections_still_prove_one_minimality(original):
    r = minimize(None, "orig", original_run=original, executor=factory(needs(lids=("L002",)), []),
                 budget={"max_attempts": 400})["result"]
    assert r["one_minimal"] and r["minimality"] == "1-minimal with respect to unit deletion"
    assert not r.get("one_minimal_unproven_units")
