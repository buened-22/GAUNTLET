# Testing

The backend test suite lives in `backend/tests/` (pytest). It contains unit tests, PostgreSQL integration
tests, API tests and end-to-end tests of the candidate pipeline that use labelled synthetic fixtures
(`backend/tests/candidates/`); no test executes AI-generated code.

## Running the tests

```bash
cd backend
python -m pytest              # everything; PostgreSQL tests skip (with a reason) if no server is reachable
python -m pytest -m "not pg"  # unit tests only (no database)
python -m pytest -m pg        # only the tests marked as needing PostgreSQL
```

* The suite collects **585 tests**; **104** carry the `pg` marker (`backend/pyproject.toml` defines the
  markers `pg` and `slow`).
* PostgreSQL tests use `GAUNTLET_PG_DSN` (default `postgresql://gauntlet:gauntlet@127.0.0.1:55432/postgres`;
  see [GETTING_STARTED.md](GETTING_STARTED.md)). If no server is reachable they are **skipped with the reason
  printed**, not failed, so a run without PostgreSQL is not a full verification. The continuous-integration
  jobs check that PostgreSQL 16 is reachable before pytest starts, so they cannot pass by skipping.
* A full run takes about 12 to 15 minutes on the development machine.

## Current results (version 0.4.1)

Verified on 2026-11-13 (author-run; not independently reproduced):

| Where | Result |
|---|---|
| Windows 11, Python 3.14.0, PostgreSQL 16.14 (development machine) | **585 passed, 0 failed, 0 skipped** (`python -m pytest`, about 12 min) |
| Windows 11, Python 3.11.18, PostgreSQL 16.14 (development machine) | **585 passed, 0 failed, 0 skipped** |
| CI: Windows Server 2025, Python 3.14, PostgreSQL 16.14 | **585 passed, 0 skipped**; demonstration script 9 of 9 |
| CI: Ubuntu 26.04.1, Python 3.11 and 3.14, PostgreSQL 16.16 from `docker-compose.yml` | **579 passed, 6 skipped** on each Python version; demonstration script 9 of 9 |
| Dashboard | `npm run typecheck` and `npm run build` pass; `npm audit` reports 0 vulnerabilities |

On Linux, six tests that check Windows job-object limits are skipped by design, with the reason printed.
The first CI runs on Linux and on Python 3.11, on an earlier build of this release, found two defects, both
fixed in 0.4.1: the package could not be imported on Python 3.11, and the actor-protocol reader accepted
JSON values that were not objects (see [../CHANGELOG.md](../CHANGELOG.md)). Nothing has been run on macOS.

## Continuous integration

`.github/workflows/ci.yml` runs on every push to `main` and `gauntlet/**`, on pull requests and on demand:

* **Frozen evidence checksums**: `sha256sum -c` over `evidence/*.sha256`;
* **Backend (Linux)**: Python 3.11 and 3.14, PostgreSQL 16 from `docker-compose.yml`, the pinned
  dependencies, the full suite and `scripts/run_demo.sh`;
* **Backend (Windows)**: Python 3.14, the portable PostgreSQL 16 cluster (`gauntlet pg install|init|start`),
  the full suite and the demonstration script;
* **Dashboard**: `npm ci`, typecheck and build.

## Known sources of spurious failures

Before 0.4.1, full runs on the development machine ended with one failure (510 of 511, then 511 of 512). It
was the `< 10 s` wall-clock bound in `test_row_and_byte_budgets_stop_the_statement_without_buffering_it`
(audit finding FR-9, previously attributed to disk load). Its query put `generate_series` in `FROM`, which
PostgreSQL materializes into a 668 MB temporary file before returning the first row; that took about 11 s on
an idle machine, so the bound measured the disk and not the harness's result budget. The query now streams
its rows (0.2 s); the bound and the assertions are unchanged.

* Other tests still contain wall-clock bounds and can fail on a heavily loaded machine.
* A machine that goes to sleep during the run also causes failures: cleanup connections time out across the
  suspension, and the leak checks then report a test database that was not dropped. Keep the machine awake
  for the 12 to 15 minutes the suite takes.

## Tests required by the specification

* `test_oracle_detects_wrong_final_state_even_when_app_reported_success`
* `test_inconclusive_run_is_never_reported_as_pass`
* `test_infra_error_is_never_pass`
* `test_unreachable_database_is_infra_error_not_pass`

All four are in `backend/tests/`.

## Other verification

* **Frozen evidence:** `(cd evidence && sha256sum -c *.sha256)` prints four OK lines
  ([REPRODUCIBILITY.md](REPRODUCIBILITY.md)).
* **Benchmark re-execution:** `scripts/compare_benchmark.py` compares a re-execution with the frozen
  evidence cell by cell, read-only; on 2026-11-12 the v0.4.1 tree matched the frozen v0.3.0 benchmark in 180
  of 180 cells ([REPRODUCIBILITY.md](REPRODUCIBILITY.md)).
* **Demonstration:** `bash scripts/run_demo.sh` (9 checks; never executes candidate code).

## History

The tagged v0.4.0 release had 409 tests (67 need PostgreSQL). Last recorded result for v0.3.0 (Windows 11,
Python 3.14.0, PostgreSQL 16.14), run by the author: **231 passed, 0 skipped, 0 failed**. (v0.2.0 had 215
tests and v0.1.0 had 75.) That breaks down as 207 tests that need no database:

* money, linearizability checker, ledger, oracle verdicts;
* oracle operation identity by idempotency key;
* schedule determinism, seed-driven schedule diversity, and preservation of each scenario's fault
  opportunities across 30 seeds, including S04's post-backfill repeat of `expand`;
* registry consistency, API validation, report generation;
* conventional-suite reporting that keeps pass, fail and infrastructure error distinct.

And 24 PostgreSQL integration tests:

* references pass;
* m1, m2 and m3 are killed by their intended invariants;
* a reused idempotency key under a new operation id passes the reference and fails m3;
* same-seed reproducibility;
* lock-wait detection;
* crash before and after commit;
* lost response followed by an idempotent retry;
* the full API run lifecycle with comparison;
* persistence;
* an unreachable database reported as `infra_error`;
* R2 coverage of the post-backfill repeat: both references pass S04, and a test-only implementation whose
  repeated `expand` erases `total_cents` fails R2 (it would have passed under the v0.2.0 S04 schedule and is
  not in the benchmark registry).
