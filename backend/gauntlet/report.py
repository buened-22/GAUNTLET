"""Markdown report generation from recorded run / benchmark data only."""

from __future__ import annotations

from collections import Counter

from gauntlet.impls.registry import IMPLEMENTATIONS
from gauntlet.oracle.invariants import BY_ID
from gauntlet.scenarios.catalog import SCENARIOS

LIMITATIONS = [
    "Interleavings are explored at SQL-statement granularity under a single-threaded controller; behaviour "
    "that depends on true parallel execution inside PostgreSQL (e.g. simultaneous lock acquisition) is not explored.",
    "Each schedule is either scripted or drawn from a seeded random policy; there is no systematic or "
    "coverage-guided search of the interleaving space.",
    "The linearizability oracle may place an uncertain operation's effect anywhere in the union of its "
    "attempts' windows, which is slightly more permissive than the real constraint.",
    "Relaxation-based diagnoses (S1/S2/S4/S6) mean 'consistent with', not proof of root cause.",
    "Determinism assumes the same PostgreSQL version and configuration; deadlock-victim choice and other "
    "timing-dependent server behaviour are not controlled.",
    "Results cover reference implementations and hand-written mutants only. No external AI agent has been evaluated.",
]


def _fmt_status(s: str) -> str:
    return {"pass": "PASS", "fail": "FAIL", "inconclusive": "INCONCLUSIVE", "not_applicable": "n/a",
            "infra_error": "INFRA ERROR"}.get(s, s)


def run_report(run: dict) -> str:
    sc = SCENARIOS.get(run["scenario_id"])
    impl = IMPLEMENTATIONS.get(run["impl_id"])
    m = run.get("metrics", {})
    lines = [
        f"# Gauntlet run report `{run['id']}`", "",
        f"*Generated from recorded run data. Benchmark version {run['benchmark_version']}.*", "",
        "## Configuration", "",
        "| Field | Value |", "|---|---|",
        f"| Scenario | `{run['scenario_id']}`: {sc.title if sc else '?'} |",
        f"| Implementation | `{run['impl_id']}` ({run['impl_kind']}, strategy {run['strategy']}) |",
        f"| Seed | {run['seed']} |",
        f"| Created | {run['created_at']} |",
        f"| Environment | Python {run['environment'].get('python')}, PostgreSQL {run['environment'].get('postgres', '?')}, "
        f"{run['environment'].get('platform')} |",
        f"| Verdict | **{_fmt_status(run['verdict'])}** |",
        f"| Duration | {run['duration_ms'] / 1000:.2f} s (wall clock; not part of the logical result) |",
        "",
    ]
    if impl and impl.kind == "mutant":
        lines += [f"Mutation: {impl.mutation}  ", f"Intended invariant: `{impl.intended_invariant}`", ""]
    if sc:
        lines += ["## Scenario", "", sc.purpose, "", f"Expected behaviour: {sc.expected_behavior}", ""]
    lines += [
        "## Reproducibility", "",
        f"- Schedule hash: `{run['schedule_hash']}` (function of scenario, seed and benchmark version only)",
        f"- Logical trace hash: `{run['trace_hash']}` (statement-level trace, timings excluded)",
        f"- Outcome hash: `{run['outcome_hash']}`",
        f"- Re-run: `python -m gauntlet run --scenario {run['scenario_id']} --impl {run['impl_id']} --seed {run['seed']}`",
        "",
        "## Event trace summary", "",
        f"{m.get('events', 0)} events, {m.get('statements', 0)} granted SQL statements, {m.get('attempts', 0)} "
        f"client/migration attempts ({m.get('acknowledged', 0)} acknowledged, {m.get('failed', 0)} failed, "
        f"{m.get('uncertain', 0)} uncertain), {m.get('faults_injected', 0)} fault-tagged events, "
        f"{m.get('crashes', 0)} process crashes, {m.get('lock_waits', 0)} lock waits, "
        f"{m.get('recoveries', 0)} recovery actions, {m.get('snapshots', 0)} oracle snapshots.", "",
    ]
    kinds = Counter(e["kind"] for e in run.get("events", []))
    if kinds:
        lines += ["| Event kind | Count |", "|---|---|"] + [f"| {k} | {v} |" for k, v in sorted(kinds.items())] + [""]
    faults = [e for e in run.get("events", []) if e.get("fault")]
    if faults:
        lines += ["Injected faults:", ""]
        lines += [f"- seq {e['seq']}: `{e['fault']}` {e['kind']} on {e['actor']}"
                  + (f" ({e.get('attempt_id')})" if e.get("attempt_id") else "") for e in faults[:40]]
        lines += [""]
    lines += ["## Invariant outcomes", "", "| Invariant | Status | Summary | Trigger seq |", "|---|---|---|---|"]
    for r in run["invariants"]:
        lines.append(f"| `{r['id']}` | {_fmt_status(r['status'])} | {r['summary'].replace('|', '/')} | "
                     f"{r.get('trigger_seq') or ''} |")
    lines.append("")
    for r in run["invariants"]:
        if r["status"] == "fail":
            lines += [f"### `{r['id']}`: {BY_ID[r['id']].title}", "", f"{r['summary']}", ""]
            for ev in r["evidence"][:5]:
                lines.append(f"- `{_short(ev)}`")
            lines.append("")
    if run.get("infra_error"):
        lines += ["## Infrastructure error", "", f"```\n{run['infra_error']}\n```", ""]
    if run.get("inconclusive"):
        lines += ["## Inconclusive conditions", ""] + [f"- {x}" for x in run["inconclusive"]] + [""]
    lines += ["## Limitations", ""] + [f"- {x}" for x in LIMITATIONS] + [""]
    return "\n".join(lines)


def _short(obj, n: int = 400) -> str:
    s = str(obj)
    return s if len(s) <= n else s[: n - 3] + "..."


def _conventional_facts(s: dict, impl: str) -> dict | None:
    """pass / fail / infra_error facts for one implementation, kept distinct.

    Derived from the recorded per-test statuses (present in every stored summary), so older summaries
    whose `failed_tests` also listed infrastructure errors are rendered correctly too.
    """
    rec = (s.get("conventional") or {}).get(impl)
    if not rec:
        return None
    tests = rec["tests"]
    failed = [t["id"] for t in tests if t["status"] == "fail"]
    infra = [t["id"] for t in tests if t["status"] == "infra_error"]
    passed = sum(1 for t in tests if t["status"] == "pass")
    outcome = "failed" if failed else "inconclusive" if infra else "passed"
    return {"passed": passed, "total": len(tests), "failed": failed, "infra": infra, "outcome": outcome}


def _conventional_cell(f: dict | None) -> str:
    if f is None:
        return "-"
    parts = [f"{f['passed']}/{f['total']} pass"]
    if f["failed"]:
        parts.append(f"fails {', '.join(f['failed'])}")
    if f["infra"]:
        parts.append(f"infrastructure error in {', '.join(f['infra'])}, not evaluated")
    return parts[0] + (f" ({'; '.join(parts[1:])})" if len(parts) > 1 else "")


def benchmark_report(summary: dict, title: str = "Gauntlet benchmark results") -> str:
    s = summary
    rep = s["reproducibility"]
    lines = [
        f"# {title}", "",
        f"*Generated by `python -m gauntlet bench` from benchmark `{s.get('benchmark_id')}` "
        f"(benchmark version {s['benchmark_version']}, PostgreSQL {s.get('environment', {}).get('postgres', '?')}). "
        "Every number below comes from a recorded run in the results database. Nothing was edited by hand.*", "",
        "## Headline numbers", "",
        "| Metric | Value |", "|---|---|",
        f"| Scenarios executed | {s['scenarios_executed']} |",
        f"| Implementations | {s['implementations']} ({len(s['references'])} reference, {s['mutants_total']} mutants) |",
        f"| Seeds per (implementation, scenario) | {', '.join(map(str, s['seeds']))} |",
        f"| Runs completed (matrix) | {s['runs_completed']} |",
        f"| Run verdicts | pass {s['verdicts']['pass']}, fail {s['verdicts']['fail']}, "
        f"inconclusive {s['verdicts']['inconclusive']}, infra error {s['verdicts']['infra_error']} |",
        f"| Invariant violations (FAIL results across all runs) | {s['invariant_violations']} |",
        f"| Faulty variants detected | {s['mutants_detected']} / {s['mutants_total']} |",
        f"| Faulty variants killed by their intended invariant | {s['mutants_killed_as_intended']} / {s['mutants_total']} |",
        f"| Inconclusive runs | {s['inconclusive_runs']} |",
        f"| Infrastructure failures | {s['infrastructure_failures']} |",
        f"| Reproducibility re-runs identical (schedule, trace, outcome, verdict) | {rep['reproduced']} / {rep['checked']} |",
        "",
        "## Reference implementations", "",
        "| Implementation | Adversarial runs passed | Conventional tests |", "|---|---|---|",
    ]
    for r in s["references"]:
        lines.append(f"| `{r['impl_id']}` | {r['passed']} / {r['runs']} | "
                     f"{_conventional_cell(_conventional_facts(s, r['impl_id']))} |")
    lines += [""]
    for r in s["references"]:
        for np in r["non_pass"]:
            lines.append(f"- `{r['impl_id']}` did not pass `{np['scenario']}` seed {np['seed']}: {np['verdict']} (run `{np['run']}`)")
    lines += ["", "## Mutant validation", "",
              "A mutant is *killed as intended* when its intended invariant FAILs in at least one run of one of "
              "its intended scenarios without any infrastructure error.", "",
              "| Mutant | Intended invariant | Intended-scenario detection (runs) | Killed as intended | "
              "Other invariants failed (any scenario) | Conventional suite |",
              "|---|---|---|---|---|---|"]
    facts = {m["impl_id"]: _conventional_facts(s, m["impl_id"]) for m in s["mutants"]}
    for m in s["mutants"]:
        lines.append(f"| `{m['impl_id']}` | `{m['intended_invariant']}` | {m['intended_detection']} | "
                     f"{'yes' if m['killed_as_intended'] else 'NO'} | "
                     f"{', '.join(m['other_invariants_failed']) or '-'} | {_conventional_cell(facts[m['impl_id']])} |")
    passing_conv = [m["impl_id"] for m in s["mutants"]
                    if m["detected"] and facts[m["impl_id"]] and facts[m["impl_id"]]["outcome"] == "passed"]
    inconclusive_conv = [m["impl_id"] for m in s["mutants"]
                         if m["detected"] and facts[m["impl_id"]] and facts[m["impl_id"]]["outcome"] == "inconclusive"]
    lines += ["", "### Passes the conventional suite but fails adversarial execution", "",
              "Listed only when every conventional test passed.", ""]
    lines += ([f"- `{x}`" for x in passing_conv] or ["- none"]) + [""]
    if inconclusive_conv:
        lines += ["### Conventional result inconclusive (infrastructure errors) but fails adversarial execution", "",
                  "No conventional test failed for these implementations, but at least one test ended in an "
                  "infrastructure error and was not evaluated, so they are neither counted as passing nor as "
                  "failing the conventional suite.", ""]
        lines += [f"- `{x}`: infrastructure error in {', '.join(facts[x]['infra'])}" for x in inconclusive_conv] + [""]

    lines += ["## Detection matrix (runs with verdict FAIL / runs)", ""]
    scen = sorted({sc for cells in s["matrix"].values() for sc in cells})
    lines += ["| Implementation | " + " | ".join(f"`{x.split('_')[0]}`" for x in scen) + " |",
              "|---|" + "---|" * len(scen)]
    for impl, cells in s["matrix"].items():
        row = []
        for sc in scen:
            c = cells.get(sc)
            if not c:
                row.append("-")
                continue
            extra = []
            if c["inconclusive"]:
                extra.append(f"{c['inconclusive']} inc")
            if c["infra_error"]:
                extra.append(f"{c['infra_error']} infra")
            row.append(f"{c['fail']}/{c['runs']}" + (f" ({', '.join(extra)})" if extra else ""))
        lines.append(f"| `{impl}` | " + " | ".join(row) + " |")
    lines += ["", "Scenario key: " + "; ".join(f"`{x.split('_')[0]}` = {x}" for x in scen), ""]

    lines += ["## Failed invariants per cell", ""]
    for impl, cells in s["matrix"].items():
        for sc, c in cells.items():
            if c["failed_invariants"]:
                inv = ", ".join(f"{k} ({v})" for k, v in sorted(c["failed_invariants"].items()))
                lines.append(f"- `{impl}` × `{sc}`: {inv}")
    lines += ["", "## Reproducibility", "",
              "Each (implementation, scenario) pair was re-executed with its first seed; the table reports "
              "whether the schedule hash, logical trace hash, outcome hash and verdict were identical.", "",
              "| Implementation | Scenario | Seed | Schedule | Trace | Outcome | Verdict |", "|---|---|---|---|---|---|---|"]
    for x in rep["details"]:
        yn = lambda b: "same" if b else "DIFFERENT"  # noqa: E731
        lines.append(f"| `{x['impl_id']}` | `{x['scenario_id']}` | {x['seed']} | {yn(x['schedule_hash'])} | "
                     f"{yn(x['trace_hash'])} | {yn(x['outcome_hash'])} | {yn(x['verdict'])} |")
    if s.get("conventional"):
        lines += ["", "## Conventional suite (per test)", ""]
        tests = [t["id"] for t in s["conventional_tests"]]
        lines += ["| Implementation | " + " | ".join(tests) + " |", "|---|" + "---|" * len(tests)]
        for impl, c in s["conventional"].items():
            st = {t["id"]: t["status"] for t in c["tests"]}
            lines.append(f"| `{impl}` | " + " | ".join("pass" if st.get(t) == "pass" else
                                                      st.get(t, "-").upper() for t in tests) + " |")
        lines += ["", "Tests: " + "; ".join(f"{t['id']} = {t['title']}" for t in s["conventional_tests"])]
    lines += ["", "## Limitations of these results", ""] + [f"- {x}" for x in LIMITATIONS] + [""]
    return "\n".join(lines)
