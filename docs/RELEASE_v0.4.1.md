# Release v0.4.1: notes

**GAUNTLET v0.4.1: audit hardening and first public release (research prototype)**

v0.4.1 is a patch release on top of [v0.4.0](RELEASE_v0.4.0.md). It contains security hardening of the
external-candidate pipeline, fixes found by an audit and by the first runs on Linux and Python 3.11, and the
documentation for publishing the repository. It adds no evaluation capability.

**No benchmark scenario, oracle rule, reference implementation, faulty variant or recorded result changed.**
The benchmark version stays `0.3.0`, the frozen evidence in `evidence/` is byte-for-byte unchanged, and a
re-execution of the 180-cell benchmark with this version matches the frozen evidence in every cell (verdict,
schedule, trace and outcome hashes, invariant statuses).

## Read this first

> **GAUNTLET does not sandbox untrusted code.** `process` mode is a development sandbox for code you
> trust, not a security boundary. `strict` (container / VM) isolation is not implemented and always refuses
> to run. To evaluate AI-generated or otherwise untrusted code, run GAUNTLET inside a disposable virtual
> machine with no network and no shared folders. See [../SECURITY.md](../SECURITY.md).

Nothing in this release changes that. The hardening below reduces what a defective or hostile candidate can
do to the harness and to its results; it does not make it safe to run one on your machine.

## Security hardening

* **Harness-owned database connections (audit finding A-01).** The process that hosts candidate code has no
  database connection, DSN, credentials or driver. The harness executes each statement after the controller
  grants it: one statement per request, with a bounded result.
* **Non-superuser observation (A-02).** A candidate's run database is read by a per-run observer role with
  a pinned `search_path`, never by a superuser session. A candidate that makes its database unobservable is
  recorded as a candidate failure, never as a pass.
* **Evidence-based timeout classification (FR-3).** A statement that outlasts the step timeout is
  attributed to the candidate only when the recorded evidence supports it.
* **API request-origin checks.** The local API refuses requests that a web page could relay through the
  operator's browser (DNS rebinding, cross-site requests). Before, such a page could start a candidate
  evaluation and supply the operator's trust declaration. The API still has no authentication and is for
  local use only.
* **Isolation record.** Every candidate evaluation lists the controls that were applied and the ones that
  were not enforced.

### Fixed by a security audit before this release

A security readiness audit of the release candidate found two problems. Both were fixed before v0.4.1 was
published, and the fixes were verified by repeating the audit's probes:

* **The dashboard's development server exposed local files (HIGH).** With the vite 5.4.11 pinned until then,
  any website open in the operator's browser could, while `npm run dev` was running, read any file the
  operator can read and read API responses through the development server's proxy. Vite is now 6.4.6
  (`npm audit`: 0 vulnerabilities), and `frontend/vite.config.ts` listens on `localhost` only, sends no CORS
  headers, serves only the dashboard's own files and passes the browser's `Host` header to the API. Every
  audit probe is now refused, and the dashboard still works. The API's own checks were not at fault: the
  old development server granted CORS to every origin and rewrote the `Host` header of proxied requests.
  State-changing API requests were refused throughout.
* **The documentation overstated the Python audit-hook guard (MEDIUM).** It said only native code could
  bypass the guard. A few lines of plain Python switch most of it off, and such a candidate passes static
  validation. The documentation and the isolation record now describe the guard as a tripwire for trusted
  code, not a control, and new tests pin that behaviour, including that the Windows job object's limits
  still hold after the guard is switched off.

The audit's other findings are prototype limitations that the documentation already states (no execution
mode is a security boundary, the local API has no authentication, the local PostgreSQL superuser password is
a published development default) or improvements planned for later releases. `certifi`, which only the test
client uses, is pinned to a release without a known advisory.

## Fixes

* **Python 3.11.** The package could not be imported on Python 3.11, although it declares `>= 3.11`.
* **Malformed protocol lines.** A line from an actor that was valid JSON but not an object ended the run as
  a harness exception instead of the actor's protocol violation. It was never a pass.
* **`COPY`.** A statement that started `COPY` to or from the client left the actor's session unusable. It is
  now an error of that one statement.
* **A test that timed the disk (FR-9).** One test's time bound measured a 668 MB server-side temporary
  file instead of the harness's result budget. Its query now streams; the bound is unchanged.
* Two tests were made portable to Linux.

## Licensing

* The statement path uses only psycopg's public interfaces. An unreleased development version had adapted a
  few lines of psycopg's LGPL-licensed code; that code was never part of a release and has been replaced.
* A `NOTICE` file states the copyright, and [THIRD_PARTY.md](THIRD_PARTY.md) lists the dependencies and
  their licences. The repository itself contains no third-party source code.

## Added

* `scripts/run_demo.sh`: a one-command demonstration that never executes candidate code.
* `scripts/compare_benchmark.py`: compares a benchmark re-execution with the frozen evidence.
* Continuous integration on Windows and Linux.
* The record of one AI-generated candidate evaluated once
  ([../research/gemini-attempt2-results.md](../research/gemini-attempt2-results.md)). It is a demonstration of
  the pipeline on one attempt and one seed. It is not a study, and it supports no statement about how often
  any model produces such a defect. The candidate's source and the raw run records are not in the
  repository.

## Verification

Run by the author from 2026-11-12 to 2026-11-14; nothing here has been independently reproduced.

| Check | Result |
|---|---|
| Test suite, Windows 11, Python 3.14.0, PostgreSQL 16.14 | 585 passed, 0 failed, 0 skipped |
| Test suite, Windows 11, Python 3.11.18, PostgreSQL 16.14 | 585 passed, 0 failed, 0 skipped |
| Test suite in CI, Windows Server 2025, Python 3.14, PostgreSQL 16.14 | 585 passed, 0 skipped |
| Test suite in CI, Ubuntu 26.04.1 (GitHub's `ubuntu-latest` runner), Python 3.11 and 3.14, PostgreSQL 16.16 (`docker-compose.yml`) | 579 passed, 6 skipped on each Python version (the six check Windows job-object limits and are skipped by design) |
| Benchmark re-execution against `evidence/results-bench-eed0c47427.sqlite3` | 180 of 180 cells identical |
| Evidence checksums | 4 of 4 |
| `scripts/run_demo.sh` | 9 of 9 checks |
| Dashboard | typecheck and build pass; `npm audit` reports 0 vulnerabilities |
| Security probes of the audit's F-01 vectors against the development server | all refused (25 of 25 checks, including the dashboard's own use); the same probe fails 11 checks on the previous build |
| Python dependencies (`pip-audit` on `backend/constraints.txt`) | no known vulnerabilities |

## Known limitations

The full list is in [LIMITATIONS.md](LIMITATIONS.md) and [SECURITY_MODEL.md](SECURITY_MODEL.md). In short:

* No execution mode is a security boundary; `strict` isolation is not implemented.
* On Linux, `process` mode's resource limits are applied but not verified by tests, and the process-count
  limit is not enforced. The benchmark has not been re-executed on Linux. macOS has not been run.
* One task family, PostgreSQL 16, statement-level interleavings at READ COMMITTED, bounded exploration.
* Scenarios, references, faulty variants and the conventional test suite were all written by one author.
* No study of any AI coding system has been run.

## Upgrading from v0.4.0

Reinstall the package (`pip install -c backend/constraints.txt -e "backend[dev]"`). Results databases and
candidate snapshots are compatible. API clients must address the server by a loopback name and send JSON
bodies with `Content-Type: application/json`.
