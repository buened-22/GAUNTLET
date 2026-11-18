# Demo security notes

Short, presenter-facing summary of what GAUNTLET's candidate execution does and does not protect. The
authoritative description is [SECURITY_MODEL.md](SECURITY_MODEL.md); the full limitations list is
[LIMITATIONS.md](LIMITATIONS.md). State: branch `gauntlet/full-audit-hardening` at commit `2a73551`
(the audit hardening released as `v0.4.1`, on top of `v0.4.0`).

## The one sentence to say

**`process` isolation is a development sandbox for trusted code. It does not make untrusted code safe.**
GAUNTLET has no verified security boundary today, so it must not run code from an AI agent, or from any
other untrusted source, outside a disposable VM.

## Trust boundary today

| Code | Trust | Where it runs | Boundary |
|---|---|---|---|
| Harness, oracle, controller | Trusted | Harness process (operator's OS user) | n/a |
| Built-in references and mutants | Trusted (hand-written, in the repository) | Actor subprocesses; their SQL runs on harness-owned connections | None applied or claimed |
| External candidates (`ext:cand-…`) | **Must be trusted** on the host (only labelled synthetic fixtures have run there; the one Gemini-generated candidate ran only inside a disconnected VM) | Actor subprocesses under `process` mode, only with the explicit `--trusted-dev-env` declaration | **None.** Defence in depth only, listed below |

Without the trust declaration every entry point refuses to load a candidate, and `--isolation strict`
always refuses because container/VM execution is not implemented (both exit code 3; captured in
[../research/demo-transcript.md](../research/demo-transcript.md)).

## What `process` mode adds (defence in depth, not a boundary)

Verified by tests on Windows 11 only ([SECURITY_MODEL.md](SECURITY_MODEL.md), control table):

* a Windows Job Object per actor (kill-on-close, one active process, memory and CPU-time limits);
* an allow-listed environment (no API keys, tokens or database credentials) and a per-actor workspace;
* a Python audit-hook guard (writes outside the workspace, process creation, sockets, `ctypes`, the
  database driver). **It is a tripwire for trusted code, not a control: plain Python can switch most of it
  off, and native code can bypass it entirely**;
* **harness-owned database connections (audit finding A-01):** the actor process is given no connection,
  DSN or credentials (candidate code can still obtain them, see below); each SQL request is one statement,
  executed by the harness after the controller grants it, with bounded results, and recorded in the run's
  `statement_log`;
* a per-run PostgreSQL role for the candidate (owner of its run database only, not a superuser);
* **a per-run non-superuser observer role for snapshot reads (audit finding A-02):** no superuser session
  opens in an external candidate's run database; lock probes, liveness checks and backend termination use a
  control connection as the admin role to the admin DSN's database (the maintenance database by default),
  never to the run database.

## What is not protected in `process` mode

Candidate code runs as the operator's OS user and can:

* read any file that user can read, **including the oracle, scenarios and reference implementations**
  (benchmark integrity is therefore not protected against a hostile candidate);
* read the harness process's environment and memory, which hold the **PostgreSQL superuser
  credentials** (the default development password is the published `gauntlet`);
* open network connections, including a new connection to the database server as superuser;
* disable the audit-hook guard (a few lines of plain Python) or avoid it with native code;
* use any OS facility available to that user; there is no kernel or OS boundary.

Withholding credentials from the actor (A-01) and observing as a non-superuser (A-02) close specific
in-database paths. They do **not** contain hostile candidate code, so they do not make `process` mode safe.

**Note on the generated candidate summary.** Its "Applied" and "Not enforced" lines come from each run's
isolation record (`backend/gauntlet/candidates/sandbox.py`, `check()`). In commit `2a73551` and earlier, that
record predates the audit hardening: "Applied" omits the harness-owned connections (A-01) and the observer
role (A-02), and "Not enforced" omits the harness's environment and memory and a new database connection
opened by native code. A later change on this branch (after `2a73551`) corrects the record (checked by
`backend/tests/test_candidate_sandbox.py` and `backend/tests/test_candidate_pg.py`). Summaries generated
before that change, including the 2026-11-05 rehearsal's, still show the older lists.

## Audit findings: status in v0.4.1

| Finding | Status | Evidence | What remains |
|---|---|---|---|
| A-01: candidate SQL could bypass the gate on a shared connection | Addressed (commit `7c3ac9f`) | `backend/tests/test_audit_hardening_pg.py` | A *new* connection opened by native code (see above) |
| FR-3: candidate statement timeouts classified as infrastructure | Addressed (commit `58f628e`) | `backend/tests/test_step_timeout.py`, `backend/tests/test_step_timeout_pg.py` | Attribution is a heuristic; ambiguous cases stay `infrastructure` |
| A-02: superuser session reading the candidate's database | Addressed for external candidates (commit `2a73551`) | `backend/tests/test_observer_role_pg.py` | Superuser credentials remain in the harness; built-in implementations are still observed as superuser; non-superuser admin untested |
| FR-2: `process` mode is not a security boundary | **Open** | [SECURITY_MODEL.md](SECURITY_MODEL.md) | Needs a verified VM/container boundary |
| FR-9: wall-clock thresholds in some tests can fail under load | **Partly addressed** | The one test that failed in every recent full run timed a 668 MB server-side temporary file, not the harness; its query now streams (bound unchanged). Latest full suite (2026-11-12): 581 passed, 0 failed | Other tests keep wall-clock bounds and can still fail on a heavily loaded machine. Test robustness only; not a security property |
| F-01 (security audit): the dashboard's development server (vite 5.4.11) let any website read local files and proxied API responses | Addressed (v0.4.1): vite 6.4.6 and a hardened `frontend/vite.config.ts` | Probe of every audit vector: all refused; dashboard and a proxied benchmark run still work | Do not start the development server with `--host` |
| F-02 (security audit): documentation said only native code could bypass the Python audit-hook guard; plain Python can switch most of it off | Addressed (v0.4.1): documentation and the isolation record corrected | `backend/tests/test_candidate_sandbox.py` (the guard can be switched off; the OS-level limits still hold) | The guard remains a tripwire for trusted code, not a control |
| API reachable from a web page through the operator's browser (DNS rebinding, cross-site request), which could start a candidate evaluation with the trust declaration | Addressed (v0.4.1) | `backend/tests/test_api_local_guard.py` | The API still has no authentication: any local process or local user can use it |

"Addressed" means implemented and covered by the tests named, run by the author on Windows 11. None of it
has been independently reviewed.

## Before accepting arbitrary external candidates

Required (from [SECURITY_MODEL.md](SECURITY_MODEL.md#what-must-exist-before-evaluating-untrusted-ai-generated-code)):

1. A real isolation boundary (disposable VM, or a container runtime with a hardened profile) for the actor
   processes: no host filesystem access beyond the candidate snapshot and contract modules (no oracle,
   scenarios, references or other candidates), no network, limits enforced by the boundary itself, and a
   fresh environment per evaluation. Since A-01 the actor needs no database connection at all: its only
   channel is the request pipe to the harness.
2. `strict` mode implemented against that boundary, failing closed when it is unavailable.
3. Hostile-fixture tests inside the boundary with positive controls (oracle reads, sockets, fork bombs,
   memory exhaustion, connections to other databases).
4. Non-default database credentials that never enter the sandbox.
5. An independent review of the boundary and its tests.

## Phrases for the demo

| Say | Do not say |
|---|---|
| "process mode is a development sandbox, not a security boundary" | "sandboxed", "safe to run untrusted code", "isolated" |
| "A-01, FR-3 and A-02 are addressed in v0.4.1, author-tested" | "security issues resolved", "secure" |
| "one Gemini-generated candidate was evaluated once, in a disconnected VM, as a demonstration" | "evaluated Gemini" / "Gemini produces this defect" |
| "the API is local-only and unauthenticated" | "production-ready" |
