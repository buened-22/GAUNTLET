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
during the release review; nothing on POSIX has been exercised.

| Area | Control | Status |
|---|---|---|
| Process tree | Windows Job Object, kill-on-close, active-process limit 1 | Enforced and verified (Windows) |
| Memory | Job Object per-process memory limit (default 512 MB) | Enforced and verified (Windows): a 300 MB allocation fails under a 150 MB limit in evaluation, exploration, minimization and replay runs |
| CPU | Job Object per-process user-CPU limit (default 120 s) | Enforced (Windows); wall clock bounded separately by the harness run deadline and watchdog |
| Output | stderr beyond the limit is drained and counted, not stored | Enforced by the harness |
| Environment | allow-listed environment variables; no harness DSN or host secrets passed | Enforced and verified |
| Working directory | per-actor temporary workspace, removed afterwards | Enforced |
| Python-level guard | audit hook denying writes outside the workspace, process creation, sockets, `ctypes` | Enforced for pure Python only; **bypassable by native code** |
| Static validation | manifest, file types, size limits, no symlinks / junctions / reparse points, AST denylist of dangerous imports and calls | Enforced; a denylist is not a sandbox |
| Database | per-run restricted role (`gauntlet_cand_*`), owner of its run database only, not superuser, no CREATEDB / CREATEROLE; `CONNECT` and `TEMPORARY` revoked from PUBLIC on every run database | Enforced and verified: the role cannot connect to another run's database (with a positive control showing the test detects a leak) and cannot create tables, databases or roles, or alter roles, on `postgres` |
| POSIX | process group with `RLIMIT_AS` / `RLIMIT_CPU`; **no process-count limit** | Not exercised |

**Not enforced in `process` mode:**

* **Host file reads.** Candidate code runs as the operator's user and can read anything that user can
  read, including the oracle source, the scenario catalog, the reference implementations and other
  candidates' snapshots. Benchmark integrity against a candidate that reads the oracle is therefore not
  protected.
* **Network.** Native code can open sockets; the audit hook only stops pure-Python socket use.
* **Guard bypass.** Native extensions, or interpreter-level tricks, can disable or avoid the audit hook.
* **Kernel / OS escape.** There is no OS-level boundary at all.

Every run records its isolation configuration, the per-limit enforcement on that platform, the controls
applied, the controls *not* enforced, and the operator's trust declaration. The record is written from the
configuration actually used, and reports quote it.

## The API and dashboard

* The API binds to `127.0.0.1` by default and has **no authentication**. Anyone who can reach the port can
  start runs and, with `isolation.trusted_environment: true`, candidate evaluations. Never expose it on a
  network interface (`--host 0.0.0.0`) or behind a public proxy.
* CORS allows only the local Vite origins (`localhost:5173`, `127.0.0.1:5173`).
* The default local PostgreSQL cluster uses the development credentials `gauntlet` / `gauntlet` on
  `127.0.0.1:55432`. They are not secrets; do not reuse them for any server reachable from a network.
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
