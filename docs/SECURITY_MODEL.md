# Security model

GAUNTLET executes migration implementations against a PostgreSQL database. Some of those
implementations (external *candidates*) may in future be produced by AI coding agents and must then be
treated as untrusted code. This document states what is protected, against whom, and what each execution
mode does and does not guarantee. It is deliberately conservative: a control is listed as enforced only
if it has been exercised by a test on the platform named.

**Summary.** No execution mode in this version is a security boundary for untrusted code. `process` mode
is a development sandbox for trusted code. `strict` (container/VM) mode is not implemented and always
fails closed. Do not run code from an AI agent, or any other untrusted source, with GAUNTLET until the
requirements in the last section are met and independently tested.

## Assets

| Asset | Why it matters |
|---|---|
| Host files of the operating user (home directory, credentials, SSH keys, browser data) | Confidentiality of the operator's machine |
| Environment variables of the harness process (may hold `GAUNTLET_PG_DSN` with a superuser password) | Database takeover |
| Oracle source (`backend/gauntlet/oracle/`), scenario catalog, reference implementations | A candidate that reads them can tailor itself to the benchmark (benchmark integrity) |
| Results database (`GAUNTLET_RESULTS_DB`) and the frozen evidence in `evidence/` | Integrity of recorded results |
| Other runs' databases on the shared PostgreSQL server | Cross-run contamination and data exposure |
| The PostgreSQL superuser and other roles | Server takeover |
| Host CPU, memory, process table, disk | Availability |
| Network access from the host | Exfiltration, lateral movement |

## Attacker model

A **malicious or defective candidate**: arbitrary Python (and, through Python, native code) placed in a
candidate directory, loaded by the harness into its actor processes and called through the migration
contract. It may try to read or write host files, read the oracle, open network connections, start
processes, exhaust resources, connect to other databases, escalate database privileges, or tamper with
the results database. It may also simply be wrong (hang, crash, allocate unboundedly).

Out of scope: an attacker with a shell on the host, a malicious operator, a compromised Python
interpreter or PostgreSQL build, and supply-chain attacks on GAUNTLET's own dependencies.

## Execution modes

### Built-in implementations (references, mutants)

Hand-written, part of the repository, trusted. They run in actor processes with the harness's database
role. No isolation is applied or claimed.

### `strict` (container / VM)

**Not implemented.** Requesting it always returns `isolation_unavailable` and executes nothing
(verified by tests and by `python -m gauntlet candidate evaluate ... --isolation strict`, exit code 3).

### `process` (development sandbox, NOT a security boundary)

Refuses to run unless the operator explicitly declares the host a trusted, disposable development
environment (`--trusted-dev-env` on the CLI, `isolation.trusted_environment: true` in the API). Without
the declaration every entry point (single runs, benchmark, conventional suite, differential, exploration,
minimization, replay, evaluation) fails closed before the candidate module is imported; an import-time
canary in the test suite verifies that no candidate code ran.

With the declaration, the following controls apply. "Verified" means exercised by a test on Windows 11
during the release review. On POSIX the sandboxed actor path runs in continuous integration (Ubuntu), but
the tests that check its limits are skipped there, so no POSIX limit is verified.

| Area | Control | Status |
|---|---|---|
| Process tree | Windows Job Object, kill-on-close, active-process limit 1 | Enforced and verified (Windows) |
| Memory | Job Object per-process memory limit (default 512 MB) | Enforced and verified (Windows): a 300 MB allocation fails under a 150 MB limit in evaluation, exploration, minimization and replay runs |
| CPU | Job Object per-process user-CPU limit (default 120 s) | Enforced (Windows); wall clock bounded separately by the harness run deadline and watchdog |
| Output | stderr beyond the limit is drained and counted, not stored | Enforced by the harness |
| Environment | allow-listed environment variables; no harness DSN or host secrets passed | Enforced and verified |
| Working directory | per-actor temporary workspace, removed afterwards | Enforced |
| Python-level guard | audit hook denying writes outside the workspace, process creation, sockets, `ctypes` and the database driver | **Not a security control.** It stops *trusted* code from using these facilities by accident. **Plain Python can switch most of it off**: the hook reads its rules from module globals that candidate code can reach through `sys.modules` and rebind, and such a candidate passes static validation (audit finding F-02; verified: socket connection, file creation outside the workspace with `os.open`, `ctypes` import). Native code can bypass it entirely. The OS-level job object limits still hold after it is switched off (verified on Windows: no child process, memory limit) |
| Static validation | manifest, file types, size limits, no symlinks / junctions / reparse points, AST denylist of dangerous imports and calls | Enforced; a denylist is not a sandbox |
| Database | per-run restricted role (`gauntlet_cand_*`), owner of its run database only, not superuser, no CREATEDB / CREATEROLE; `CONNECT` and `TEMPORARY` revoked from PUBLIC on every run database | Enforced and verified: the role cannot connect to another run's database (with a positive control showing the test detects a leak) and cannot create tables, databases or roles, or alter roles, on `postgres`. **Not** a complete database boundary (see "Opening a new database connection" below) |
| Snapshot reads (A-02) | per-run observer role (`gauntlet_obs_*`): not superuser, no CREATEDB / CREATEROLE / REPLICATION / BYPASSRLS, member of no role, `CONNECT` on its run database only, `SELECT` granted by the candidate role; `search_path` pinned to the catalog, `row_security = off`, schema-qualified queries, observed tables must be plain tables. Lock probes, liveness, backend termination and statement activity use a separate control connection to the maintenance database | Enforced and verified (since v0.4.1): no superuser session opens in an external run's database; a view replacing `orders` is not read and cannot escalate (positive control shows the same view escalates when a superuser reads it); the observer cannot create roles or databases, `ALTER SYSTEM`, `COPY … TO PROGRAM`, read `pg_authid` or signal superuser backends (`backend/tests/test_observer_role_pg.py`) |
| POSIX | process group with `RLIMIT_AS` / `RLIMIT_CPU`; **no process-count limit** | Applied in CI runs on Ubuntu; enforcement **not verified** (the limit tests are Windows-only) |

**Not enforced in `process` mode:**

* **Host file reads.** Candidate code runs as the operator's user and can read anything that user can
  read, including the oracle source, the scenario catalog, the reference implementations and other
  candidates' snapshots. Benchmark integrity against a candidate that reads the oracle is therefore not
  protected.
* **Network.** Candidate code can open sockets. The audit hook blocks pure-Python socket use only until the
  candidate switches the hook off, which plain Python can do.
* **Guard bypass.** Plain Python can disable most of the audit hook by rebinding the module globals it reads
  its rules from; native extensions and other interpreter-level tricks can disable or avoid it entirely.
  Static validation does not detect this.
* **Kernel / OS escape.** There is no OS-level boundary at all.
* **Opening a new database connection (residual of audit finding A-01).** The shared-connection bypass is
  closed: the harness process owns every actor's database connection, the actor process holds no
  connection, no DSN and no credentials, does not load the database driver, and every statement it issues
  is executed by the harness only after the controller granted it (statement class and transaction state
  are the harness's own observations). Each request is exactly one SQL statement: the harness uses the
  extended query protocol, so the server rejects a string with several statements before running any of
  it. Results are fetched row by row within a row and byte budget (`MAX_RESULT_ROWS`, `MAX_RESULT_BYTES`);
  a larger result cancels the statement and fails the run as a candidate execution failure. Verified from
  inside a sandboxed candidate process and by checking that every executed statement is in the run's
  `statement_log` and every client statement in the trace (`backend/tests/test_audit_hardening_pg.py`).
  What remains: candidate code (plain Python that has switched the guard off, or native code) can open a
  *new* socket to the database server
  and log in with credentials it knows or can obtain: the published development default (`gauntlet` /
  `gauntlet`, see below), and, because process mode runs the candidate as the same operating-system user as
  the harness, any credentials it can read from the harness process's environment or memory. The guard
  denies importing the driver and opening sockets only until the candidate switches it off; that is
  defense in depth, not isolation.
  Non-default credentials alone do not close this in process mode; it needs network and process isolation
  (strict mode in a VM or container).
* **Harness sessions and the candidate's database (audit finding A-02; addressed in
  v0.4.1).** In v0.4.0 the harness read the candidate-owned run database as the bootstrap
  superuser, so a candidate-created object (for example a view replacing `orders` that calls a function)
  could run with superuser privileges during a snapshot. For external candidates the harness now reads
  snapshots as the per-run observer role described in the table above, and runs lock probes, liveness
  checks and backend termination on a control connection to the maintenance database, so no superuser
  session opens in the run database. What remains:
  * the candidate can still make its database unobservable (revoke the observer's access, enable
    row-level security, replace or drop a table). The run then ends as a `candidate_execution_failure`,
    never a pass;
  * candidate code that somehow ran inside the observer's session would run with the observer's few
    privileges (no read path is known to call candidate code once the checks above pass);
  * the control connection is still a superuser session, on the maintenance database, and the harness still
    holds the admin credentials, which native candidate code can read in `process` mode (see above);
  * built-in implementations (trusted) are still observed by a superuser session, as before;
  * a non-superuser admin (`CREATEDB` / `CREATEROLE`, README "Any other PostgreSQL 16 server") is untested.
* **Default superuser credentials.** The local development cluster's superuser password is the published
  default (`gauntlet`), so code that can open native sockets can log in as superuser.
* **Database-server resources.** The result budget (`MAX_RESULT_ROWS`, `MAX_RESULT_BYTES`) bounds what the
  harness buffers, not what the server does before it returns the first row. A statement that makes
  PostgreSQL materialize a large intermediate result (for example a set-returning function in `FROM`)
  writes it to temporary files first: `SELECT g FROM generate_series(1, 50000000) g` wrote a 668 MB
  temporary file in about 11 s on the development machine before the row budget could stop it. Such a
  statement is bounded only by the step timeout (default 20 s) and the run deadline; no `temp_file_limit`,
  `work_mem` or disk quota is set for the candidate's role. A candidate can therefore consume disk and I/O
  on the database server.
* **Step-timeout classification (audit finding FR-3; since v0.4.1).** When a
  statement neither completes nor blocks on a lock within the step timeout (`GAUNTLET_STEP_TIMEOUT_S`,
  default 20 s), the harness gathers evidence, then ends the statement and the actor (the session is closed
  or terminated, so its transaction rolls back and its locks are released). The actor is sent nothing, so
  no wall-clock outcome enters the trace. The run is classified `candidate_statement_timeout` only if
  **all** of the following hold; otherwise it stays `infrastructure`:
  * the run is an external candidate's (built-in implementations are never attributed);
  * the harness's own wait loop never stalled for more than `GAUNTLET_STEP_STALL_GAP_S` (default 2 s);
  * the server answered a query about the statement and reported it active for (almost) the whole step,
    either on CPU or in `pg_sleep`; I/O, lightweight-lock, client and other waits do not qualify;
  * a cancel request then ended it with SQLSTATE 57014, or it completed, within
    `GAUNTLET_STEP_CANCEL_GRACE_S` (default 5 s). SQLSTATE 57014 alone is never sufficient.

  The evidence is recorded in the run's notes. The rule is a heuristic: a host that slows the server but not
  the harness can still make a CPU-bound statement look like the candidate's fault. A run-level time limit
  that has passed takes precedence (`timeout`). Neither outcome is ever a pass.

Every run records its isolation configuration, the per-limit enforcement on that platform, the controls
applied, the controls *not* enforced, and the operator's trust declaration. The record is written from the
configuration actually used, and reports quote it.

## The API and dashboard

* The API binds to `127.0.0.1` by default and has **no authentication**. Anyone who can reach the port can
  start runs and, with `isolation.trusted_environment: true`, candidate evaluations. Never expose it on a
  network interface (`--host 0.0.0.0`) or behind a public proxy; `gauntlet serve` prints a warning when it
  is given a host that is not a loopback address.
* **Requests relayed by a web browser are refused** (`backend/gauntlet/api/local_guard.py`, verified by
  `backend/tests/test_api_local_guard.py`, including a control showing the same requests succeed without
  the check). Binding to the loopback interface does not keep web pages out, because the operator's own
  browser can reach it on a page's behalf. Before this check existed, a page using DNS rebinding could list
  the registered candidates and start an evaluation with `trusted_environment: true`, making the operator's
  trust declaration for them. The API now refuses:
  * any request whose `Host` header is not a loopback name (`127.0.0.1`, `localhost`, `::1`), which is what
    a rebinding page sends (403);
  * state-changing requests (anything but `GET`, `HEAD`, `OPTIONS`) whose `Origin` is neither the local
    dashboard nor the API server itself (403);
  * request bodies that are not `application/json` (415). Earlier FastAPI releases, which
    `backend/pyproject.toml` still permits, parse a body sent without a `Content-Type` as JSON, and a
    cross-site page can send such a body without a CORS preflight.

  `GAUNTLET_API_ALLOWED_HOSTS` and `GAUNTLET_API_ALLOWED_ORIGINS` (comma-separated) add names. These checks
  are **not authentication**: any local process, and any other user of the same machine, can still use the
  API. Do not register an untrusted candidate on a host where the API is running.
* CORS allows only the local Vite origins (`localhost:5173`, `127.0.0.1:5173`).
* **The dashboard's development server** (`frontend/`, `npm run dev`) is reachable by any web page the
  operator has open, like the API. It is configured not to help such a page (`frontend/vite.config.ts`):
  it listens on `localhost` only, sends no CORS headers (the dashboard is same-origin and needs none),
  serves only files inside `frontend/` (`server.fs.strict`), and vite refuses requests whose `Host` is not
  a local name. Its `/api` proxy keeps the browser's `Host` header (`changeOrigin: false`), so the API's own
  checks above still apply to proxied requests. With the vite 5.4.11 pinned before v0.4.1 was finalized,
  a page could read any file the operator can read and read API responses through the proxy (security
  audit finding F-01); vite 6.4.6 together with this configuration refuses every probe of that audit,
  while the dashboard itself keeps working. A browser still delivers a proxied `GET` response to a foreign
  page's request, but without a CORS grant the page cannot read it.
* The default local PostgreSQL cluster uses the development credentials `gauntlet` / `gauntlet` on
  `127.0.0.1:55432`, for a superuser. They are not secrets and not suitable for production: do not reuse
  them for any server reachable from a network, and on a machine shared with other user accounts any of
  them can log in to the local cluster as superuser.
* Credentials in DSNs are redacted in experiment reports.

## Results and evidence integrity

* Run records, benchmark summaries and analyses are hashed in experiment manifests;
  `GET /api/experiments/<id>/verify` re-hashes them. The manifest lives in the same database, so this
  detects accidental change, not deliberate tampering by someone who can also rewrite the manifest. Keep an
  external copy of the export bundle hash (`/api/experiments/<id>/export`) for tamper evidence.
* Every GAUNTLET command, including read-only ones such as `report` and `serve`, adds missing tables to the
  results database it opens. Never point `GAUNTLET_RESULTS_DB` at a file in `evidence/`; work on a copy. The
  frozen evidence is protected by its `.sha256` files and by git.

## What must exist before evaluating untrusted (AI-generated) code

1. A real isolation boundary: a disposable VM, or a container runtime with a hardened profile, in which
   the candidate's actor processes run, with
   * no access to the host filesystem except a read-only copy of the candidate snapshot and the harness
     contract modules, and **no copy of the oracle, scenarios, references or other candidates**;
   * no network except a single connection to its own run database (enforced outside the sandbox, e.g. a
     network policy or a socket proxy);
   * process-count, memory, CPU, disk and wall-clock limits enforced by the boundary itself;
   * a fresh, discarded environment per evaluation.
2. `strict` mode implemented against that boundary, failing closed when the boundary is unavailable.
3. Tests that execute deliberately hostile fixtures inside the boundary and verify each property above
   (file reads of the oracle, socket connections, fork bombs, memory exhaustion, connections to other
   databases), including positive controls showing each test detects a breach.
4. An independent review of the boundary and its tests by someone other than the author.

Until then, only trusted synthetic fixtures (`backend/tests/candidates/`) may be run, and only in
`process` mode with the explicit trust declaration.

## Reporting a vulnerability

See [SECURITY.md](../SECURITY.md).
