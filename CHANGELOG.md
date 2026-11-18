# Changelog

All notable changes to GAUNTLET are recorded here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

GAUNTLET carries two version numbers, and they move independently:

* the **software version** (`backend/pyproject.toml`, git tags), which this file tracks;
* the **benchmark version** (`BENCHMARK_VERSION` in `backend/gauntlet/__init__.py`, currently `0.3.0`),
  which is part of every schedule hash and changes only when a scenario, oracle rule, reference or faulty
  variant changes.

No release after 0.3.0 changed a benchmark scenario, oracle rule, reference implementation, faulty variant
or recorded result. The frozen evidence in `evidence/` is byte-for-byte unchanged since it was recorded.

## [Unreleased]

Documentation only. No code, test, dependency, configuration, workflow, scenario, oracle rule, reference,
faulty variant, recorded result or evidence file changed.

### Changed

* The README is shorter and links to new reference documents: [docs/README.md](docs/README.md)
  (documentation index), [docs/GETTING_STARTED.md](docs/GETTING_STARTED.md), [docs/API.md](docs/API.md)
  (every implemented endpoint), [docs/TESTING.md](docs/TESTING.md) and
  [docs/RESULTS_OVERVIEW.md](docs/RESULTS_OVERVIEW.md). Their content was moved from the README.
* The setup instructions install the pinned dependency versions (`-c backend/constraints.txt`) by default.
* Working records from the v0.4.1 development (the release-readiness audit, the A-02/FR-3 remediation plan,
  the demonstration notes, script, transcript and rehearsal report) moved unchanged to
  [docs/archive/](docs/archive/README.md).
* New [docs/SECURITY_FINDINGS.md](docs/SECURITY_FINDINGS.md): which audit each finding identifier comes from
  and where its status is recorded, including the two unrelated uses of `F-02`, and the known
  `opentelemetry-api` advisory.

### Fixed

* `docs/HISTORY.md` said the results records, the v0.1.0 self-audit and the audit scripts had checksums in
  `evidence/*.sha256`; those files cover only the evidence databases and the conventional re-run record.
* `research/results-summary.md` said no AI-generated candidate had been evaluated; it now records the one
  demonstration evaluation of 2026-11-07.
* `PROJECT_SPEC.md` described the v0.3.0 implementation status as current; it now covers v0.4.0 and v0.4.1.
* `research/open-questions.md` did not mention the demonstration evaluation.

### Known issues

* `opentelemetry-api` 1.45.1 (CVE-2026-126360 / GHSA-mf5v-w3w3-f6cg) is still pinned; the fix, 1.45.2, is
  planned for the next engineering release ([docs/SECURITY_FINDINGS.md](docs/SECURITY_FINDINGS.md)).

## [0.4.1] - 2026-11-16

Audit hardening on top of v0.4.0, and the first public release. No benchmark scenario, oracle rule,
reference, faulty variant or recorded result changed: a re-execution of the benchmark on this version
matches the frozen v0.3.0 evidence in all 180 cells. Release notes:
[docs/RELEASE_v0.4.1.md](docs/RELEASE_v0.4.1.md). The commit identifiers in this section are those of the
private development branch; this repository contains the release as one commit
([docs/HISTORY.md](docs/HISTORY.md)).

### Security

* **Dashboard development server (security audit finding F-01).** With the previously pinned vite 5.4.11,
  any website the operator had open could, while `npm run dev` was running, read local files through the
  development server and read API responses through its `/api` proxy, which granted CORS to every origin
  and rewrote the `Host` header. Vite is now pinned to 6.4.6 (with esbuild 0.25.12; `npm audit`: 0
  vulnerabilities), and `frontend/vite.config.ts` listens on `localhost` only, sends no CORS headers, serves
  only the dashboard's own files and keeps the browser's `Host` header on proxied requests, so the API's own
  checks apply. Every probe of the audit is refused; the dashboard and proxied runs still work.
* **Accurate description of the Python audit-hook guard (security audit finding F-02).** The documentation
  and the isolation record of every evaluation said only native code could bypass the guard. Plain Python
  can switch most of it off by rebinding the module globals it reads its rules from, and such a candidate
  passes static validation. The guard is now described as what it is, a tripwire for trusted code and not
  a control; tests pin that behaviour and show that the Windows job object's limits still hold after the
  guard is switched off. The guard's behaviour is unchanged.
* `certifi` pinned to 2026.10.18 in `backend/constraints.txt` (PYSEC-2026-4320). GAUNTLET makes no HTTPS
  requests; `certifi` arrives only through the test client's `httpx`.
* **API request-origin checks.** The local API now rejects requests whose `Host` header is not a loopback
  name, state-changing requests that carry a foreign `Origin`, and request bodies that are not
  `application/json`. Before this, a web page open in the operator's browser could reach the API through
  DNS rebinding and start a candidate evaluation with `trusted_environment: true`, bypassing the operator's
  trust declaration for any candidate already registered on the host. Additional host names can be allowed
  with `GAUNTLET_API_ALLOWED_HOSTS`. The API still has no authentication and is for local use only.
* **A-01: harness-owned database connections** (`7c3ac9f`). The harness process owns every actor's
  database connection. The actor process has no connection, DSN, credentials or database driver; each
  request is exactly one SQL statement, executed by the harness after the controller grants it, with
  bounded results (`GAUNTLET_MAX_RESULT_ROWS`, `GAUNTLET_MAX_RESULT_BYTES`).
* **A-02: non-superuser observer role** (`2a73551`). An external candidate's run database is read by a
  per-run observer role with a pinned `search_path`, `row_security = off` and read-only transactions. Lock
  probes and backend termination use a control connection to the maintenance database, so no superuser
  session opens in a database the candidate owns. A candidate that makes its database unobservable is a
  `candidate_execution_failure`, never a pass.
* **Isolation record** (`b4d4ef6`). Every candidate evaluation summary lists the controls that were applied
  and the ones that were not enforced.

`process` isolation is still **not** a security boundary, and `strict` (container / VM) isolation is still
not implemented and fails closed. See [SECURITY.md](SECURITY.md) and
[docs/SECURITY_MODEL.md](docs/SECURITY_MODEL.md).

### Changed

* **Statements are executed through psycopg's public interfaces only.** The harness-side statement path
  (`backend/gauntlet/actor/connection.py`) no longer subclasses psycopg's cursor. It binds parameters and
  reads results itself through `psycopg.pq` and `psycopg.adapt`. An unreleased development version had
  overridden a private cursor method with an adapted copy of psycopg's own (LGPL-3.0) code; that code was
  never in a release and is gone. On a corpus of 136 statements the old and new paths give identical
  results, except for the wording of client-side messages about malformed placeholders and for `COPY`
  (below).
* **FR-3: step-timeout classification from evidence** (`58f628e`). A statement that outlasts the step
  timeout is classified `candidate_statement_timeout` only when the recorded evidence supports it;
  otherwise it stays an infrastructure error. Neither outcome is ever a pass.
* Robustness: actor connect timeout, bounded monitor lock wait, recorded connection-close failures,
  bounded protocol-violation messages, more conservative SQL statement classification.

### Fixed

* **`COPY` wedged an actor's session.** A statement that started `COPY ... TO STDOUT` or `COPY ... FROM
  STDIN` left the connection in copy mode, and every later statement of that actor failed with "another
  command is already in progress". `COPY` to or from the client is now an error of that one statement, and
  the session stays usable.
* **FR-9: a test measured disk speed instead of the result budget.**
  `test_row_and_byte_budgets_stop_the_statement_without_buffering_it` used a query that PostgreSQL
  materializes to disk before returning its first row, so its `< 10 s` bound depended on host disk load.
  The query now streams its rows; the bound and every assertion are unchanged.
* **Python 3.11 could not import the package.** `reprlib.Repr()` was called with keyword arguments that
  exist only from Python 3.12, although `requires-python` is `>= 3.11`. Found by the first CI run.
* **Wrong attribution of a malformed protocol line.** The actor-protocol reader accepted any valid JSON. A
  line that was not a JSON object (`[1]`, `123`, `null`) made the run end as a harness exception instead of
  the actor's protocol violation. It was never a pass. Found by the first full-suite run on Linux.
* A test that keeps its run database for inspection removed the candidate role afterwards but not the
  observer role, leaving one unused `gauntlet_obs_*` role on the test server per run of the suite.
* Linux portability of two tests in `backend/tests/test_candidate_contract.py` (directory-symlink cleanup;
  owner read and write permission in the snapshot-tampering test).

### Added

* `scripts/run_demo.sh`: a one-command demonstration that runs real engine functionality and never executes
  candidate code.
* `scripts/compare_benchmark.py`: compares a benchmark re-execution with the frozen evidence cell by cell,
  read-only. The current tree matches the frozen v0.3.0 benchmark in 180 of 180 cells (2026-11-12).
* [research/gemini-attempt2-results.md](research/gemini-attempt2-results.md): the record of one
  Gemini-generated candidate evaluated once, inside a disconnected VM (2026-11-07). It is a demonstration
  of the pipeline, not a study of any model.
* `NOTICE` (copyright notice) and [docs/THIRD_PARTY.md](docs/THIRD_PARTY.md) (dependencies and their
  licences).
* `CHANGELOG.md`, `CODE_OF_CONDUCT.md` and a continuous-integration workflow (`.github/workflows/ci.yml`):
  the full suite and the demonstration on Windows (Python 3.14) and on Linux (Python 3.11 and 3.14, PostgreSQL
  16 from `docker-compose.yml`). Its first green run was the first full-suite run on Linux.
* `.gitattributes` keeps shell scripts on LF line endings, so `scripts/run_demo.sh` runs from a Windows clone.

## [0.4.0] - 2026-10-22

First release in this repository (tag `v0.4.0`, commit `be9b742`), under the Apache License 2.0.
Full notes: [docs/RELEASE_v0.4.0.md](docs/RELEASE_v0.4.0.md).

### Added

* Differential testing (`gauntlet diff`, `/api/differential`).
* Bounded schedule exploration (`gauntlet explore`, `/api/explorations`).
* Failure minimization (`gauntlet minimize`, `/api/minimizations`).
* Experiment reports with hashed evidence manifests (`gauntlet experiment`, `/api/experiments`).
* The external-candidate pipeline (`gauntlet candidate ...`): contract v1, static validation,
  content-addressed snapshots, a `process` development sandbox and a `strict` mode that always fails closed.
* Run wall-clock deadlines and event limits, recorded as timeouts and never as passes.
* Dashboard pages for the new analyses; research and security documentation; pinned dependency versions
  (`backend/constraints.txt`).

### Fixed

* Minimization could claim 1-minimality for a removal that had never been judged
  (see [docs/RELEASE_v0.4.0.md](docs/RELEASE_v0.4.0.md)).

## Earlier releases (development repository only)

Versions 0.1.0 to 0.3.0 were developed in a separate, unpublished repository whose history is not part of
this one. Their commit identifiers are preserved in [docs/HISTORY.md](docs/HISTORY.md), and their frozen
benchmark evidence is in [evidence/](evidence/).

### [0.3.0] - 2026-10-19

* Scenario S04 gains a second `repeat(expand)` after the backfill, so invariant R2 can detect a repeated
  `expand` that destroys stored values. No verdict changed.
* Conventional-suite reporting keeps pass, fail and infrastructure error distinct.
* Frozen benchmark `bench-eed0c47427` (180 runs).

### [0.2.0]

* The oracle identifies a logical write by its idempotency key.
* Seeds vary schedule structure, not only amounts, in S01, S03, S04 and S05.
* Frozen benchmark `bench-7c4395ec2b`.

### [0.1.0]

* First benchmark, `bench-f5316f9d4b`.
