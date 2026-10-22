"""Experiment reporting: metric calculations against a hand-verified fixture, completeness, versioning,
traceability, reproducibility of reports, redaction (no database needed; temporary results store).

Hand-verified fixture (7 runs):

    ref_a_trigger      S02 seed 1 pass        ref_a_trigger      S02 seed 2 pass
    m1_stale_backfill  S02 seed 1 FAIL (S1)   m1_stale_backfill  S02 seed 2 inconclusive
    m3_retry_dup.      S03 seed 1 pass        m3_retry_dup.      S03 seed 2 infra_error
    m4_premature_ctr.  S05 seed 1 FAIL (S7 only; intended C1)

Expected by hand:
    M1  pass 3, fail 2, inconclusive 1, infra 1
    M2  2 / 5 (conclusive = 3 pass + 2 fail)            M3  2 / 7
    M4  detected mutants {m1, m4} / {m1, m3, m4} = 2/3
    M5  killed by intended invariant {m1} / {m1, m3, m4} = 1/3
    M6  0 / 2 reference runs
    paired ref_a_trigger vs m1 on S02: seed 1 only-B-fails, seed 2 non-conclusive
"""

import pytest

from gauntlet.experiment import (compute, collect, export_bundle, generate_report, new_experiment, redact,
                                 revise_experiment, wilson)
from gauntlet.store import Store

FIXTURE = [
    ("r1", "ref_a_trigger", "reference", "S02_backfill_vs_legacy_writes", 1, "pass", []),
    ("r2", "ref_a_trigger", "reference", "S02_backfill_vs_legacy_writes", 2, "pass", []),
    ("r3", "m1_stale_backfill", "mutant", "S02_backfill_vs_legacy_writes", 1, "fail", ["S1_ACKED_DURABLE", "S7_LINEARIZABLE"]),
    ("r4", "m1_stale_backfill", "mutant", "S02_backfill_vs_legacy_writes", 2, "inconclusive", []),
    ("r5", "m3_retry_duplicates", "mutant", "S03_lost_ack_and_retry", 1, "pass", []),
    ("r6", "m3_retry_duplicates", "mutant", "S03_lost_ack_and_retry", 2, "infra_error", []),
    ("r7", "m4_premature_contract", "mutant", "S05_premature_finalization", 1, "fail", ["S7_LINEARIZABLE"]),
]


@pytest.fixture
def store(tmp_path):
    st = Store(tmp_path / "exp.sqlite3")
    for rid, impl, kind, sc, seed, verdict, failed in FIXTURE:
        invs = [{"id": i, "status": "fail", "trigger_seq": 3} for i in failed] + [{"id": "S5_RECORDS_PRESERVED", "status": "pass"}]
        st.save_run({"id": rid, "created_at": f"2026-01-0{seed}T00:00:{rid[1]}0", "benchmark_version": "0.3.0",
                     "scenario_id": sc, "impl_id": impl, "impl_kind": kind, "seed": seed, "verdict": verdict,
                     "schedule_hash": f"s{sc}{seed}", "trace_hash": f"t{rid}", "outcome_hash": f"o{rid}",
                     "duration_ms": 1000.0 * int(rid[1]), "invariants": invs,
                     "infra_error": "PostgreSQL unreachable at postgresql://gauntlet:hunter2@127.0.0.1:55432/postgres"
                     if verdict == "infra_error" else None,
                     "events": [{"seq": i, "kind": "stmt", "t_ms": 1.0} for i in range(1, 6)]})
    return st


def exp(store, **kw):
    cfg = {"title": "fixture", "selection": {"run_ids": [r[0] for r in FIXTURE]}, **kw}
    return new_experiment(store, cfg)


def metrics(store, eid):
    m = generate_report(store, eid)["results"]["metrics"]
    return {x["id"]: x for x in m}


def test_metric_values_match_hand_calculation(store):
    m = metrics(store, exp(store))
    assert m["M1"]["counts"] == {"pass": 3, "fail": 2, "inconclusive": 1, "infra_error": 1, "missing": 0}
    assert (m["M2"]["numerator"], m["M2"]["denominator"], m["M2"]["value"]) == (2, 5, 0.4)
    assert (m["M3"]["numerator"], m["M3"]["denominator"]) == (2, 7)
    assert (m["M4"]["numerator"], m["M4"]["denominator"]) == (2, 3)
    assert m["M4"]["numerator_ids"] == ["m1_stale_backfill", "m4_premature_contract"]
    assert (m["M5"]["numerator"], m["M5"]["denominator"]) == (1, 3) and m["M5"]["numerator_ids"] == ["m1_stale_backfill"]
    assert (m["M6"]["numerator"], m["M6"]["denominator"]) == (0, 2)
    assert m["M2"]["breakdown"] == {"reference": {"numerator": 0, "denominator": 2}, "mutant": {"numerator": 2, "denominator": 3}}


def test_denominators_trace_to_exact_runs_and_non_conclusive_runs_are_never_passes(store):
    m = metrics(store, exp(store))
    assert m["M2"]["numerator_ids"] == ["r3", "r7"] and m["M2"]["denominator_ids"] == ["r1", "r2", "r3", "r5", "r7"]
    assert "r4" not in m["M2"]["denominator_ids"] and "r6" not in m["M2"]["denominator_ids"]
    assert m["M1"]["ids"]["inconclusive"] == ["r4"] and m["M1"]["ids"]["infra_error"] == ["r6"]


def test_no_intervals_for_a_fixed_set_and_wilson_when_declared(store):
    m = metrics(store, exp(store))
    assert m["M2"]["interval_95"] is None and "not estimated" in m["M2"]["interval_method"]
    assert m["M2"]["small_sample"]
    m2 = metrics(store, exp(store, sampling="independent_random"))
    assert m2["M2"]["interval_95"] == wilson(2, 5)
    lo, hi = wilson(2, 5)
    assert lo == pytest.approx(0.1176, abs=1e-3) and hi == pytest.approx(0.7693, abs=1e-3)  # textbook Wilson for 2/5


def test_paired_comparison_counts(store):
    rep = generate_report(store, exp(store))
    p = next(x for x in rep["results"]["paired"] if x["b"] == "m1_stale_backfill")
    assert p["a"] == "ref_a_trigger" and p["only_b_fails"] == 1 and p["unmatched_or_non_conclusive"] == 1
    assert p["both_pass"] == 0 and p["both_fail"] == 0 and p["only_a_fails"] == 0


def test_exclusions_are_applied_and_listed(store):
    m = metrics(store, exp(store, exclusions=[{"run_id": "r7", "reason": "configuration error found before analysis"}]))
    assert (m["M2"]["numerator"], m["M2"]["denominator"]) == (1, 4)
    assert m["M1"]["excluded"] == [{"run_id": "r7", "reason": "configuration error found before analysis"}]


def test_completeness_follows_the_stopping_rule(store):
    rep = generate_report(store, exp(store, required={"impls": ["ref_a_trigger", "m1_stale_backfill"],
                                                     "scenarios": ["S02_backfill_vs_legacy_writes"], "seeds": [1, 2, 3]}))
    c = rep["results"]["completeness"]
    assert c["status"] == "incomplete" and c["cells_required"] == 6 and c["cells_conclusive"] == 3
    assert sorted(map(tuple, c["missing_cells"])) == [("m1_stale_backfill", "S02_backfill_vs_legacy_writes", 3),
                                                      ("ref_a_trigger", "S02_backfill_vs_legacy_writes", 3)]
    assert c["unresolved_cells"][0][:3] == ["m1_stale_backfill", "S02_backfill_vs_legacy_writes", 2]
    assert "incomplete" in rep["markdown"]
    assert generate_report(store, exp(store))["results"]["completeness"]["status"] == "not_assessed"


def test_unavailable_data_is_marked_not_invented(store):
    m = metrics(store, exp(store))
    assert m["M7"]["value"] is None and "unavailable" in m["M7"]["value_note"]
    assert m["M8"]["value"] is None and m["M11"]["value"] is None
    rep = generate_report(store, exp(store))
    assert "No external AI coding agent was evaluated" in rep["markdown"]


def test_report_is_reproducible_from_the_same_data(store):
    eid = exp(store)
    a, b = generate_report(store, eid), generate_report(store, eid)
    assert a["markdown"] == b["markdown"] and a["markdown_sha256"] == b["markdown_sha256"]
    assert a["results"] == b["results"]


def test_manifest_references_actual_persisted_artifacts(store):
    rep = generate_report(store, exp(store))
    arts = {a["id"]: a for a in rep["manifest"]["artifacts"] if a["type"] == "run"}
    assert set(arts) == {r[0] for r in FIXTURE}
    for rid, a in arts.items():
        assert a["record_sha256"] == store.run_record_sha256(rid)
    assert "M2" in arts["r3"]["supports_metrics"] and "M2" not in arts["r4"]["supports_metrics"]
    assert rep["manifest"]["runs_not_referenced"] == []


def test_secrets_are_redacted_everywhere(store):
    eid = exp(store, title="creds postgresql://admin:s3cret@db:5432/x api_key=abcd1234 C:\\Users\\someone\\x")
    bundle = export_bundle(store, eid)
    import json
    text = json.dumps(bundle)
    assert "s3cret" not in text and "abcd1234" not in text and "hunter2" not in text and "someone" not in text
    assert redact("password: hunter2") == "password: ***"
    assert redact("postgresql://u:p@h/db") == "postgresql://***:***@h/db"
    assert bundle["bundle_sha256"] and bundle["format"] == "gauntlet-experiment-bundle/1"


def test_versioning_and_deviations(store):
    eid = new_experiment(store, {"title": "prereg", "selection": {"run_ids": ["r1"]}, "preregistered": True,
                                 "experiment_type": "confirmatory"})
    with pytest.raises(ValueError):
        revise_experiment(store, eid, {"title": "changed"}, reason=None)
    doc = revise_experiment(store, eid, {"selection": {"run_ids": ["r1", "r2"]}}, reason="added a run that was missed")
    assert [v["version"] for v in doc["versions"]] == [1, 2]
    assert doc["versions"][0]["config"]["selection"]["run_ids"] == ["r1"], "original plan preserved"
    assert doc["deviations"][0]["reason"] == "added a run that was missed"
    with pytest.raises(ValueError):
        new_experiment(store, {"title": "x", "selection": {"run_ids": ["r1"]}, "experiment_type": "confirmatory"})
    with pytest.raises(ValueError):
        new_experiment(store, {"title": "x", "selection": {}})


def test_changes_after_results_are_recorded_as_deviations(store):
    eid = exp(store)
    generate_report(store, eid)
    with pytest.raises(ValueError):
        revise_experiment(store, eid, {"exclusions": [{"run_id": "r3", "reason": "inconvenient"}]}, reason=None)
    doc = revise_experiment(store, eid, {"exclusions": [{"run_id": "r6", "reason": "harness outage"}]}, reason="harness outage")
    assert doc["deviations"][-1]["after_results_observed"] is True


def test_benchmark_selection_reproducibility_and_conventional_metrics(store):
    """Summary in the exact format bench.py writes: r1..r3, r5, r7 are matrix runs; r2 and r4 are re-runs of
    r1 and r3 (r4 did not reproduce). Hand-verified: M8 = 1/2; re-runs excluded from M2; M7 = 3/4 (ref CT03 infra
    excluded: ref 2 pass of 2 decided, m1 1 pass of 2 decided)."""
    st = store
    with st._conn() as c:  # put the fixture runs into one benchmark
        c.execute("UPDATE runs SET benchmark_id = 'b1'")
    st.create_benchmark("b1", "2026-01-01", {"impls": ["ref_a_trigger"], "scenarios": ["S02_backfill_vs_legacy_writes"], "seeds": [1]})
    rep = lambda a, b, ok: {"impl_id": "x", "scenario_id": "s", "seed": 1, "run_a": a, "run_b": b,  # noqa: E731
                            "schedule_hash": True, "trace_hash": ok, "outcome_hash": True, "verdict": True, "reproduced": ok}
    tests = lambda *st_: [{"id": f"CT0{i + 1}", "status": s} for i, s in enumerate(st_)]  # noqa: E731
    st.finish_benchmark("b1", "2026-01-02", {
        "runs": [{"id": i} for i in ("r1", "r3", "r5", "r6", "r7")],
        "reproducibility": {"checked": 2, "reproduced": 1, "details": [rep("r1", "r2", True), rep("r3", "r4", False)]},
        "conventional": {"ref_a_trigger": {"tests": tests("pass", "pass", "infra_error")},
                         "m1_stale_backfill": {"tests": tests("pass", "fail")}}})
    m = metrics(st, new_experiment(st, {"title": "b", "selection": {"benchmark_id": "b1"}}))
    assert (m["M8"]["numerator"], m["M8"]["denominator"]) == (1, 2) and m["M8"]["numerator_ids"] == ["r2"]
    assert (m["M7"]["numerator"], m["M7"]["denominator"]) == (3, 4) and "ref_a_trigger:CT03" in m["M7"]["treatment_of_non_conclusive"]
    assert "r2" not in m["M2"]["denominator_ids"] and "r4" not in m["M1"]["ids"]["inconclusive"]
    assert {e["run_id"] for e in m["M1"]["excluded"]} == {"r2", "r4"}


def test_collect_rejects_unknown_runs(store):
    with pytest.raises(KeyError):
        collect(store, {"selection": {"run_ids": ["nope"]}, "exclusions": []})
