# Results summary

Everything GAUNTLET has measured to date, separated by what was evaluated and by outcome type. Every
number below comes from a recorded run, and each was rechecked during the v0.4.0 release review
(2026-10-22). All validation was run by the author on one Windows 11 machine (Python 3.14.0, PostgreSQL
16.14). None of it has been independently reproduced.

## What was evaluated

| Category | Implementations | Evaluated? |
|---|---|---|
| Hand-written reference implementations | `ref_a_trigger` (trigger synchronisation), `ref_b_dualwrite` (atomic dual-write + post-drain reconciliation) | Yes: frozen benchmarks v0.1.0–v0.3.0, demo |
| Hand-written single-point mutants | m1–m8, each with a documented intended failure | Yes: frozen benchmarks, demo |
| **Synthetic external-candidate fixtures** | `synthetic_ref_a_copy`, `synthetic_stale_backfill` (defect injected deliberately), `synthetic_import_canary` (test-only). Manually adapted from the references; **not AI-generated** | Yes, as trusted code in `process` mode with the explicit trust declaration |
| **Real external AI-generated candidates** | none | **No.** None has been evaluated, because no verified isolation boundary exists |

## Frozen benchmark v0.3.0 (`bench-eed0c47427`, `evidence/results-bench-eed0c47427.sqlite3`)

Configuration: benchmark version 0.3.0; 6 scenarios × 10 implementations × seeds 1–3; 60 reproducibility
re-runs; the 10-test conventional suite for each implementation.

| Outcome type | Count | Meaning |
|---|---|---|
| Runs (matrix cells) | 180 | 180 distinct (scenario, implementation, seed) cells; recomputed from the raw run rows |
| Passing runs | 121 | every invariant pass or not applicable, at least one pass |
| Runs with a **confirmed invariant violation** | 59 | the oracle reported FAIL for at least one invariant, with no infrastructure error |
| Invariant-level FAIL entries | 108 | FAIL statuses summed over invariants in those 59 runs; a count of violated (run, invariant) pairs, **not** of distinct defects |
| **Inconclusive** runs | 0 | |
| **Infrastructure errors** (matrix) | 0 | never counted as pass or fail |
| Reference runs passing | 36/36 | |
| Mutants killed by their *intended* invariant in an intended scenario | 8/8 | recomputed per mutant from the run rows |
| Reproducibility re-runs identical (schedule, trace, outcome hash, verdict) | 60/60 | |
| Mutants passing all 10 conventional tests but failing adversarial runs | 4 (m1, m3, m7, m8) | |
| Mutants whose conventional result is inconclusive | 1 (m2): conventional test CT03 ended in an **infrastructure error** of unknown cause | a later supplementary re-run passed 10/10; it is a separate record and does not replace this result |

Historical benchmarks v0.2.0 and v0.1.0 are kept unchanged in `evidence/` with their own result files.
They are not run-for-run comparable with v0.3.0 (the benchmark version is part of every schedule hash).

## Analyses (synthetic demonstration, re-run 2026-10-22)

| Analysis | Configuration | Result | Outcome type |
|---|---|---|---|
| Differential | `ref_a_trigger` vs `m1_stale_backfill`, S02, seed 1 | `confirmed_violation`: m1 fails S1_ACKED_DURABLE and S7_LINEARIZABLE, the reference passes | confirmed invariant violation + differential disagreement |
| Differential | `ref_a_trigger` vs `ref_b_dualwrite`, S02, seed 1 | `agreement` (their only difference is migration bookkeeping, normalized by rule N3) | no disagreement; **agreement is not a correctness claim** |
| Exploration | `F01_backfill_race_probe` × m1, ≤ 1 preemption | completed: 18 distinct schedules, 13 pass, 5 fail, 1 failure class (S1 + S7, `lost_ack`), replay reproduced | exhaustive **only within the stated bound** |
| Minimization | m1's S02 failing run, target S1_ACKED_DURABLE | 104 → 19 directives, 54 → 3 operations, 390 → 51 trace events; 1-minimal w.r.t. unit deletion; independent replay matches the failure signature; original run unchanged | |
| Replay | m1's S02 run | schedule, trace and outcome hashes and verdict identical; the hashes are also identical to an independent execution of the same configuration in an earlier session | |
| Candidate evaluation | `synthetic_stale_backfill`, S01 + S02, seed 1, vs `ref_a_trigger` | S01 `agreement`; S02 `confirmed_violation` (S1, S7); exploration and minimization as above; experiment report evidence 5/5 unchanged | synthetic fixture, trusted |
| Fail-closed checks | evaluation without the trust declaration; `--isolation strict` | both **blocked** (exit code 3), no candidate code executed | |

Captured output: [demo-transcript.md](demo-transcript.md).

## What these results do and do not establish

* They establish that, on this task, the instrument separates two hand-written correct strategies from
  eight hand-written defective ones for the intended reasons, reproducibly, and that the analyses behave
  as documented.
* They do not establish anything about AI coding agents, the rate at which agents produce such defects, or
  the instrument's behaviour on defects unlike the mutants.
* Detection is schedule-dependent in places (for example m5 in S05 and m6 in S02/S05 escape in one of three
  seeds); passing a scenario is evidence about the executed schedules only.
* The oracle, scenarios, references and mutants share one author (see
  [../docs/RESEARCH_METHODOLOGY.md](../docs/RESEARCH_METHODOLOGY.md#10-threats-to-validity-limitations-and-open-questions)).
