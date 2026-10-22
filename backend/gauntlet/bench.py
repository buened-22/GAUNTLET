"""Benchmark driver: implementation x scenario x seed matrix, reproducibility
re-runs, conventional suite, and mutant validation."""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Callable

from gauntlet import BENCHMARK_VERSION
from gauntlet.config import PG_ADMIN_DSN
from gauntlet.conventional import TESTS as CONVENTIONAL_TESTS
from gauntlet.conventional import run_suite
from gauntlet.engine.runner import execute_run
from gauntlet.impls.registry import IMPLEMENTATIONS, get_info
from gauntlet.pg.provision import check_server
from gauntlet.scenarios.catalog import SCENARIOS
from gauntlet.store import Store


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def run_benchmark(impls: list[str] | None = None, scenarios: list[str] | None = None,
                  seeds: list[int] | None = None, *, reproducibility: bool = True, conventional: bool = True,
                  store: Store | None = None, admin_dsn: str = PG_ADMIN_DSN,
                  progress: Callable[[str], None] = print) -> dict:
    impls = impls or list(IMPLEMENTATIONS)
    scenarios = scenarios or list(SCENARIOS)
    seeds = seeds or [1, 2, 3]
    store = store or Store()
    bench_id = "bench-" + uuid.uuid4().hex[:10]
    config = {"impls": impls, "scenarios": scenarios, "seeds": seeds, "reproducibility": reproducibility,
              "conventional": conventional, "benchmark_version": BENCHMARK_VERSION}
    store.create_benchmark(bench_id, _now(), config)
    server = check_server(admin_dsn)

    runs: list[dict] = []
    total = len(impls) * len(scenarios) * len(seeds)
    k = 0
    for impl in impls:
        for sc in scenarios:
            for seed in seeds:
                k += 1
                r = execute_run(sc, impl, seed, admin_dsn)
                store.save_run(r, benchmark_id=bench_id)
                runs.append(_brief(r))
                progress(f"[{k}/{total}] {impl} {sc} seed={seed}: {r['verdict']} "
                         f"fail={[i['id'] for i in r['invariants'] if i['status'] == 'fail']} "
                         f"({r['duration_ms'] / 1000:.1f}s)")

    repro = []
    if reproducibility:
        for impl in impls:
            for sc in scenarios:
                first = next(r for r in runs if r["impl_id"] == impl and r["scenario_id"] == sc)
                again = execute_run(sc, impl, first["seed"], admin_dsn)
                store.save_run(again, benchmark_id=bench_id)
                same = {h: first[h] == again[h] for h in ("schedule_hash", "trace_hash", "outcome_hash")}
                same["verdict"] = first["verdict"] == again["verdict"]
                repro.append({"impl_id": impl, "scenario_id": sc, "seed": first["seed"], "run_a": first["id"],
                              "run_b": again["id"], **same, "reproduced": all(same.values())})
                progress(f"[repro] {impl} {sc}: {'identical' if all(same.values()) else same}")

    conv = {}
    if conventional:
        for impl in impls:
            conv[impl] = run_suite(impl, admin_dsn)
            progress(f"[conventional] {impl}: {conv[impl]['counts']}")

    summary = summarize(runs, repro, conv, impls, scenarios, seeds)
    summary["benchmark_id"] = bench_id
    summary["environment"] = {"postgres": server["server_version"]}
    summary["config"] = config
    store.finish_benchmark(bench_id, _now(), summary)
    return summary


def _brief(r: dict) -> dict:
    return {"id": r["id"], "impl_id": r["impl_id"], "impl_kind": r["impl_kind"], "scenario_id": r["scenario_id"],
            "seed": r["seed"], "verdict": r["verdict"], "schedule_hash": r["schedule_hash"],
            "trace_hash": r["trace_hash"], "outcome_hash": r["outcome_hash"],
            "statuses": {i["id"]: i["status"] for i in r["invariants"]},
            "infra_error": r["infra_error"], "duration_ms": r["duration_ms"]}


def conventional_summary(conv_res: dict) -> dict:
    """Per-implementation conventional-suite summary that keeps pass / fail / infra_error distinct.

    outcome: "passed" (every test passed), "failed" (at least one test failed), or "inconclusive"
    (no test failed, but at least one ended in an infrastructure error and so was not evaluated).
    """
    tests = conv_res["tests"]
    failed = [t["id"] for t in tests if t["status"] == "fail"]
    infra = [t["id"] for t in tests if t["status"] == "infra_error"]
    outcome = "failed" if failed else "inconclusive" if infra else "passed"
    return {"counts": conv_res["counts"], "failed_tests": failed, "infra_error_tests": infra, "outcome": outcome}


def summarize(runs: list[dict], repro: list[dict], conv: dict, impls: list[str], scenarios: list[str],
              seeds: list[int]) -> dict:
    verdicts = {v: sum(1 for r in runs if r["verdict"] == v) for v in ("pass", "fail", "inconclusive", "infra_error")}
    violations = sum(1 for r in runs for s in r["statuses"].values() if s == "fail")

    # Matrix cell = per (impl, scenario): verdict counts and union of failed invariants.
    matrix: dict[str, dict[str, dict]] = {}
    for r in runs:
        cell = matrix.setdefault(r["impl_id"], {}).setdefault(r["scenario_id"], {
            "runs": 0, "fail": 0, "pass": 0, "inconclusive": 0, "infra_error": 0, "failed_invariants": {}})
        cell["runs"] += 1
        cell[r["verdict"]] += 1
        for inv, st in r["statuses"].items():
            if st == "fail":
                cell["failed_invariants"][inv] = cell["failed_invariants"].get(inv, 0) + 1

    references = []
    for impl in impls:
        info = get_info(impl)
        if info.kind != "reference":
            continue
        mine = [r for r in runs if r["impl_id"] == impl]
        references.append({"impl_id": impl, "runs": len(mine),
                           "passed": sum(1 for r in mine if r["verdict"] == "pass"),
                           "non_pass": [{"run": r["id"], "scenario": r["scenario_id"], "seed": r["seed"],
                                         "verdict": r["verdict"]} for r in mine if r["verdict"] != "pass"],
                           "conventional": conv.get(impl, {}).get("counts")})

    mutants = []
    for impl in impls:
        info = get_info(impl)
        if info.kind != "mutant":
            continue
        mine = [r for r in runs if r["impl_id"] == impl]
        intended = [r for r in mine if r["scenario_id"] in info.intended_scenarios]
        hits = [r for r in intended if r["statuses"].get(info.intended_invariant) == "fail" and not r["infra_error"]]
        any_fail = [r for r in mine if r["verdict"] == "fail"]
        unintended_only = [r for r in any_fail if r["statuses"].get(info.intended_invariant) != "fail"]
        infra = [r for r in mine if r["infra_error"]]
        other_invs = sorted({inv for r in any_fail for inv, st in r["statuses"].items()
                             if st == "fail" and inv != info.intended_invariant})
        conv_res = conv.get(impl)
        mutants.append({
            "impl_id": impl, "intended_invariant": info.intended_invariant,
            "intended_scenarios": list(info.intended_scenarios),
            "detected": bool(any_fail),
            "killed_as_intended": bool(hits),
            "intended_detection": f"{len(hits)}/{len(intended)}",
            "intended_detection_rate": (len(hits) / len(intended)) if intended else None,
            "runs_failing": len(any_fail), "runs_total": len(mine),
            "runs_failing_without_intended_invariant": len(unintended_only),
            "other_invariants_failed": other_invs,
            "infra_errors": len(infra),
            "conventional": None if conv_res is None else conventional_summary(conv_res),
            "passes_conventional_fails_adversarial": bool(conv_res and conv_res["all_passed"] and any_fail),
            # Not a pass and not a failure: no test failed but at least one could not be evaluated.
            "conventional_inconclusive_fails_adversarial": bool(
                conv_res and conventional_summary(conv_res)["outcome"] == "inconclusive" and any_fail),
        })

    return {
        "benchmark_version": BENCHMARK_VERSION,
        "scenarios_executed": len({r["scenario_id"] for r in runs}),
        "implementations": len(impls), "seeds": seeds,
        "runs_completed": len(runs), "verdicts": verdicts,
        "invariant_violations": violations,
        "inconclusive_runs": verdicts["inconclusive"], "infrastructure_failures": verdicts["infra_error"],
        "mutants_total": len(mutants), "mutants_detected": sum(1 for m in mutants if m["detected"]),
        "mutants_killed_as_intended": sum(1 for m in mutants if m["killed_as_intended"]),
        "reproducibility": {"checked": len(repro), "reproduced": sum(1 for x in repro if x["reproduced"]),
                            "details": repro},
        "conventional": {impl: {"counts": c["counts"], "tests": c["tests"]} for impl, c in conv.items()},
        "conventional_tests": [{"id": t.id, "title": t.title} for t in CONVENTIONAL_TESTS],
        "references": references, "mutants": mutants, "matrix": matrix,
        "runs": [{k: r[k] for k in ("id", "impl_id", "scenario_id", "seed", "verdict")} for r in runs],
    }
