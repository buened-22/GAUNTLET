"""Conventional-suite reporting keeps pass / fail / infra_error distinct (v0.3.0 reporting fix).

Regression for: an infra_error in a conventional test was reported as "fails CTxx" and silently removed
the implementation from "passes the conventional suite but fails adversarial execution".
"""

from gauntlet.bench import conventional_summary, summarize
from gauntlet.report import benchmark_report

IDS = [f"CT{i:02d}" for i in range(1, 11)]


def suite(impl, statuses: dict[str, str]):
    """A run_suite()-shaped record: every test passes unless overridden."""
    tests = [{"id": t, "title": t, "status": statuses.get(t, "pass"), "message": ""} for t in IDS]
    counts = {k: sum(1 for x in tests if x["status"] == k) for k in ("pass", "fail", "infra_error")}
    return {"impl_id": impl, "tests": tests, "counts": counts, "all_passed": counts["pass"] == len(tests)}


def run(rid, impl, kind, verdict, statuses):
    return {"id": rid, "impl_id": impl, "impl_kind": kind, "scenario_id": "S01_baseline_migration", "seed": 1,
            "verdict": verdict, "schedule_hash": "s", "trace_hash": "t", "outcome_hash": "o", "statuses": statuses,
            "infra_error": None, "duration_ms": 1.0}


IMPLS = ["ref_a_trigger", "m1_stale_backfill", "m2_nonatomic_dual_write", "m4_premature_contract", "m6_float_cents"]
RUNS = [
    run("r0", "ref_a_trigger", "reference", "pass", {"S1_ACKED_DURABLE": "pass"}),
    run("r1", "m1_stale_backfill", "mutant", "fail", {"S1_ACKED_DURABLE": "fail"}),
    run("r2", "m2_nonatomic_dual_write", "mutant", "fail", {"S3_REPR_AGREEMENT": "fail"}),
    run("r4", "m4_premature_contract", "mutant", "fail", {"C1_COMPATIBILITY": "fail"}),
    run("r6", "m6_float_cents", "mutant", "fail", {"S2_EXACT_CENTS": "fail"}),
]
CONV = {
    "ref_a_trigger": suite("ref_a_trigger", {"CT05": "infra_error"}),          # reference with an infra error
    "m1_stale_backfill": suite("m1_stale_backfill", {}),                       # all pass
    "m2_nonatomic_dual_write": suite("m2_nonatomic_dual_write", {"CT03": "infra_error"}),  # infra only
    "m4_premature_contract": suite("m4_premature_contract", {"CT08": "fail"}),  # failure only
    "m6_float_cents": suite("m6_float_cents", {"CT06": "fail", "CT09": "infra_error"}),  # mixture
}


def build():
    s = summarize(RUNS, [], CONV, IMPLS, ["S01_baseline_migration"], [1])
    md = benchmark_report({**s, "benchmark_id": "b-test", "environment": {"postgres": "16.14"}})
    return s, md, {m["impl_id"]: m for m in s["mutants"]}


def line(md, impl):
    return next(l for l in md.splitlines() if l.startswith(f"| `{impl}` |") and "pass" in l)


def test_failure_only():
    c = conventional_summary(CONV["m4_premature_contract"])
    assert c["failed_tests"] == ["CT08"] and c["infra_error_tests"] == [] and c["outcome"] == "failed"
    s, md, m = build()
    assert "fails CT08" in line(md, "m4_premature_contract") and "infrastructure" not in line(md, "m4_premature_contract")
    assert not m["m4_premature_contract"]["passes_conventional_fails_adversarial"]


def test_infra_error_only_is_not_a_failure_and_not_a_pass():
    c = conventional_summary(CONV["m2_nonatomic_dual_write"])
    assert c["failed_tests"] == [] and c["infra_error_tests"] == ["CT03"] and c["outcome"] == "inconclusive"
    s, md, m = build()
    row = line(md, "m2_nonatomic_dual_write")
    assert "fails" not in row and "infrastructure error in CT03, not evaluated" in row and "9/10 pass" in row
    assert not m["m2_nonatomic_dual_write"]["passes_conventional_fails_adversarial"]
    assert m["m2_nonatomic_dual_write"]["conventional_inconclusive_fails_adversarial"]


def test_mixture_of_pass_fail_and_infra_error():
    c = conventional_summary(CONV["m6_float_cents"])
    assert c["failed_tests"] == ["CT06"] and c["infra_error_tests"] == ["CT09"] and c["outcome"] == "failed"
    assert c["counts"] == {"pass": 8, "fail": 1, "infra_error": 1}
    s, md, m = build()
    row = line(md, "m6_float_cents")
    assert "8/10 pass" in row and "fails CT06" in row and "infrastructure error in CT09" in row
    assert "CT09" not in row.split("fails")[1].split(";")[0]  # CT09 is not listed as a failure
    assert not m["m6_float_cents"]["conventional_inconclusive_fails_adversarial"]  # a real failure decides


def test_passes_conventional_section_and_inconclusive_section():
    s, md, m = build()
    passes = md.split("### Passes the conventional suite but fails adversarial execution")[1].split("##")[0]
    assert "`m1_stale_backfill`" in passes
    for impl in ("m2_nonatomic_dual_write", "m4_premature_contract", "m6_float_cents"):
        assert f"`{impl}`" not in passes
    inconclusive = md.split("### Conventional result inconclusive (infrastructure errors) but fails adversarial execution")[1].split("##")[0]
    assert "`m2_nonatomic_dual_write`: infrastructure error in CT03" in inconclusive
    assert "m1_stale_backfill" not in inconclusive and "m6_float_cents" not in inconclusive


def test_reference_infra_error_is_visible():
    s, md, m = build()
    ref = next(l for l in md.splitlines() if l.startswith("| `ref_a_trigger` |"))
    assert "9/10 pass" in ref and "infrastructure error in CT05" in ref


def test_infra_errors_stay_visible_in_per_test_table():
    s, md, m = build()
    table = md.split("## Conventional suite (per test)")[1]
    assert "INFRA_ERROR" in table and "FAIL" in table


def test_old_format_summary_is_rendered_correctly():
    """Summaries written before the fix listed infra errors under failed_tests and lack the new keys."""
    s, _, _ = build()
    for mm in s["mutants"]:
        if mm["conventional"]:
            mm["conventional"]["failed_tests"] = [t["id"] for t in CONV[mm["impl_id"]]["tests"] if t["status"] != "pass"]
            mm["conventional"].pop("infra_error_tests"); mm["conventional"].pop("outcome")
        mm.pop("conventional_inconclusive_fails_adversarial")
    md = benchmark_report({**s, "benchmark_id": "b-old", "environment": {"postgres": "16.14"}})
    row = line(md, "m2_nonatomic_dual_write")
    assert "fails" not in row and "infrastructure error in CT03" in row
    assert "`m2_nonatomic_dual_write`: infrastructure error in CT03" in md
