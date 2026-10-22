# Frozen benchmark evidence: `bench-eed0c47427` (benchmark version 0.3.0)

This README describes three files:

* `results-bench-eed0c47427.sqlite3`: the **corrected frozen evidence** database;
* `conventional-rerun-bench-eed0c47427.json`: a **supplementary** conventional-suite re-run, kept as a
  separate record;
* their `.sha256` files.

The report generated from the corrected database is `docs/RESULTS-v0.3.0.md`. The v0.1.0 and v0.2.0
evidence in this folder is unchanged.

## What v0.3.0 changed

v0.3.0 adds one directive to scenario S04: a second `repeat` of the completed `expand`, placed after the
backfill has populated `total_cents`. This lets R2 detect a repeated `expand` that destroys stored
values. Separately, it fixes how the conventional suite is reported, so that `pass`, `fail` and
`infra_error` stay distinct.

Compared field by field with v0.2.0 (`bench-7c4395ec2b`), every one of the 180 cells has the same verdict
and the same invariant statuses. The 150 non-S04 cells are identical in schedule, trace, outcome,
snapshots and evidence. The 30 S04 cells differ only by the inserted directive and its direct
consequences: one extra `expand` attempt, two extra snapshots, one extra R2 entry, and sequence numbers
shifted after the insertion. Final rows are identical. The benchmark version is part of every schedule
hash, so v0.2.0 and v0.3.0 results are not run-for-run interchangeable.

## Contents of the corrected frozen database (verified at packaging time)

| Item | Value |
|---|---|
| Benchmark | `bench-eed0c47427`, status `completed`, benchmark version 0.3.0, PostgreSQL 16.14. Executed 2026-10-18 17:09:44Z – 18:07:19Z. |
| Run rows | **240**: 180 matrix runs (10 implementations × 6 scenarios × seeds 1–3) and 60 same-seed reproducibility re-runs (one per implementation × scenario, seed 1). No other rows. |
| Matrix verdicts | 121 pass, 59 fail, **0 inconclusive, 0 infrastructure errors** |
| Reproducibility | 60/60 re-runs identical in schedule, trace, outcome and verdict |
| Mutants | 8/8 detected; 8/8 killed by their intended invariant |
| `PRAGMA integrity_check` | `ok` |
| SHA-256 | `e0aa50a15efd45eeec0e035d805fc2a72573676295964a5a8fb500ad75ad59d9` |

## Original diagnostic database vs corrected frozen evidence

The benchmark was originally written to `.gauntlet/results-v0.3.0.sqlite3`, the **original diagnostic
database**. Its SHA-256 is `f1206cc4af14e810019ff36789ad8cc25d6d77484e4f46d65307d2e2bc7a47c2`. It is
git-ignored and is not part of this evidence. It was left unmodified, and it remains the source of truth
for the raw records.

The original database's summary was produced by reporting code that labelled a conventional-suite
infrastructure error as a test failure. The original report generated from it
(`.gauntlet/v030-original-report/RESULTS-v0.3.0.original-generator.md`, SHA-256
`05077f7593c4aae4579690a0f865059b477f79b39fae057afda6d408c6570543`, git-ignored) therefore shows
"9/10 pass (fails CT03)" for `m2_nonatomic_dual_write`. It also leaves m2 out of the "passes conventional"
list without explanation. That report is kept only as a diagnostic artifact.

The **corrected frozen database** is a byte-level snapshot of the original, made with SQLite's backup API,
in which **only** `benchmarks.summary_json` was replaced. The replacement was recomputed by the corrected
v0.3.0 code from the original recorded data, without re-running anything. Verified at packaging time:

* all 240 run rows are identical in every column: ids, timestamps, versions, verdicts, schedule, trace and
  outcome hashes, per-run summaries, and the full run records (schedules, traces, snapshots, attempts,
  invariant results and evidence);
* the benchmark row differs only in `summary_json`;
* the schema is identical.

In the summary, only these fields changed:

* `mutants[*].conventional`: `failed_tests` now lists actual failures only, and `infra_error_tests` and
  `outcome` (`passed` / `failed` / `inconclusive`) were added. The only change in content is m2:
  `failed_tests` `[CT03]` → `[]`, `infra_error_tests` `[CT03]`, outcome `inconclusive`.
* `mutants[*].conventional_inconclusive_fails_adversarial` was added.
* `summary_provenance` was added. It records how and when the summary was recomputed, and the SHA-256 of
  the original summary.

All other summary sections are equal to the original: headline counts, matrix, references,
reproducibility, per-test conventional records, configuration and environment.

## The CT03 infrastructure error (observed; cause unknown)

In the original run, conventional test **CT03 for `m2_nonatomic_dual_write` ended in `infra_error`**. The
harness's 20-second step timeout fired because a statement registering an application instance
(`INSERT INTO app_instances …`) neither completed nor waited on a lock. This is an **observed error of the
original run**, and it stays recorded as `infra_error` in this evidence. It is not a test failure and
not a pass. m2's conventional result for this benchmark is therefore **inconclusive**, so m2 is listed
neither as passing nor as failing the conventional suite.

Its cause was **not identified**. PostgreSQL logged nothing unusual during the conventional phase.

## Supplementary conventional re-run (separate record)

`conventional-rerun-bench-eed0c47427.json` (SHA-256
`94ee9bf0fb035b41630e8f37262bffff6926c770f425e213d5735f961b5c0eb3`) records one later execution of all ten
conventional suites with the same v0.3.0 code (2026-10-19 07:17:38Z – 07:29:52Z). Each suite ran exactly
once, with no retries.

Result: **no infrastructure errors**. m4, m5 and m6 each fail exactly one test (CT08, CT07, CT06), and the
other seven implementations pass 10/10, including m2. This matches the v0.2.0 conventional results test
for test.

This re-run is **supplementary**. It does not replace or amend the benchmark's recorded conventional
results. A later successful run does not show that the original CT03 error did not happen, and does not
explain its cause.

## Verify

```bash
cd evidence
sha256sum -c results-bench-eed0c47427.sqlite3.sha256 conventional-rerun-bench-eed0c47427.json.sha256
```

On Windows without `sha256sum`, use `certutil -hashfile <file> SHA256` and compare the result with the
`.sha256` files.

## Inspect

Run from `backend/`. `report` only reads, but writing commands (`run`, `bench`, `replay`, `serve`) change
the database they are given, so point them at a copy.

```bash
GAUNTLET_RESULTS_DB="$PWD/../evidence/results-bench-eed0c47427.sqlite3" PYTHONIOENCODING=utf-8 python -m gauntlet report --benchmark
```

The command's output matches `docs/RESULTS-v0.3.0.md`, apart from one trailing newline added by
`print()`. On Windows, set `PYTHONIOENCODING=utf-8` as shown, or the console code page will mangle the "×"
characters in the report.
