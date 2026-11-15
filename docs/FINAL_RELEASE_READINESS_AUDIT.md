# GAUNTLET: final release-readiness audit (branch `gauntlet/full-audit-hardening`)

Audit date: 2026-11-05. Auditor: an AI assistant (Claude) acting for the author; this is **not** an
independent review. Everything below was checked on the author's machine; nothing was sent to or reviewed by
any third party, and no AI coding agent (Gemini or any other) has been evaluated with GAUNTLET.

## 1. Executive verdict

**READY WITH LIMITATIONS** — for an external *technical review* of a research prototype and work sample.
**NOT READY** for evaluating untrusted (AI-generated) code or any security-sensitive use.

Reasoning:

* The frozen benchmark still reproduces exactly (180/180 cells), the evidence checksums hold, protected
  paths are untouched, and the A-01 shared-connection bypass is closed with tests that fail on the old code.
* No Critical finding. Two High findings are known, documented security limitations (A-02 superuser monitor;
  `process` mode is not a boundary); they block untrusted-agent evaluation, not a code review.
* Before sending to a reviewer, three things should be fixed or decided (none requires code): A-02 is
  disclosed only in `docs/SECURITY_MODEL.md` (the technical report and README omit it, FR-4); the hardening
  is uncommitted and the GitHub repository still holds v0.4.0 without it (FR-5); two documentation
  statements are stale (FR-7, FR-8).
* One full-suite run during this audit had 3 failures from a single transient infrastructure failure under
  abnormal machine conditions; it did not reproduce (FR-6, §5).

## 2. Scope and exact state

| Item | State |
|---|---|
| Worktree | `C:\Users\<user>\gauntlet-audit-hardening` |
| Branch / HEAD | `gauntlet/full-audit-hardening` at `be9b742534a6147b6657703bc3b424a0c41e0607` (= tag `v0.4.0`); 0 commits on the branch |
| Uncommitted changes vs v0.4.0 | 14 modified tracked files (398 insertions, 167 deletions) + 4 new files (`backend/gauntlet/actor/connection.py`, `backend/gauntlet/actor/wire.py`, `backend/tests/test_audit_hardening.py`, `backend/tests/test_audit_hardening_pg.py`) + this report |
| Protected paths changed vs v0.4.0 | **0** (evidence, oracle, scenario catalog and script, references, mutants, legacy, base, registry, `docs/RESULTS*.md`, the v0.1.0 audit, `audit/`) |
| Release repository | `main` and tag `v0.4.0` at `be9b742`, clean (it holds the audit branch ref, created by `git worktree add`) |
| Original Project G | HEAD `694577b`, 81 staged, 0 unstaged, untracked `register-search.txt` (the author's own file): unchanged |

What the branch contains beyond v0.4.0 (all uncommitted): harness-owned actor database connections (A-01),
actor-protocol validation, robust cleanup, a bounded monitor lock wait, robust SQL classification, single
statement per request and bounded result fetching (M1/M2), a connect timeout, recorded connection-close
failures, `RecursionError` handling, the CRLF-safe contract test, and documentation updates.

## 3. Findings

No **Critical** findings.

The table records the findings as of the audit. For later documentation changes, see
[§11](#11-documentation-follow-up-2026-11-05).

| ID | Sev. | Location | Finding | Evidence | Impact | Recommended action | Blocks external review? |
|---|---|---|---|---|---|---|---|
| FR-1 | **High** | `backend/gauntlet/engine/runner.py:57,156`; `engine/observe.py:13-41` (unqualified `FROM orders`, line 29); `pg/localpg.py:76` | **A-02 open.** The monitor connection reads the candidate-owned run database as the cluster bootstrap superuser; objects the candidate creates (e.g. a view replacing `orders`) could run with superuser rights during a snapshot. | Static code trace (not executed: needs hostile code); disclosed in `docs/SECURITY_MODEL.md` | Defeats the per-run role restriction for a malicious candidate | Non-superuser monitor role, pinned `search_path`, schema-qualified and relkind-checked reads; keep `pg_signal_backend` for crash emulation (`engine/controller.py:95,284`) | No (blocks untrusted evaluation) |
| FR-2 | **High** | `backend/gauntlet/candidates/guard.py`, `candidates/sandbox.py`; `docs/SECURITY_MODEL.md` | **`process` mode is not a security boundary.** Native code in a candidate can read host files (including the oracle), open sockets, bypass the audit hook, and open a *new* database connection with credentials it knows or reads from the harness process (same OS user). | Documented design limitation; guard is Python-level only | Untrusted code is unsafe on the host | Verified container/VM boundary for `strict` mode before any agent study | No (blocks untrusted evaluation) |
| FR-3 | Medium | `backend/gauntlet/actor/proxy.py:434-436`; `engine/runner.py:182-186` | **Candidate-caused statement timeouts are classified `infrastructure`.** A candidate statement that runs past the step timeout (e.g. `pg_sleep`) raises a plain `InfrastructureError`, not `ActorFailure`. | Code trace | Never a pass, but a deliberately slow candidate can turn failing runs into "no verdict" runs attributed to the harness | Raise `ActorFailure(loaded=True)` for statement deadlines of external candidates; add a test | No (fix before an agent study) |
| FR-4 | Medium | `research/technical-report.md:193-200`; `README.md:434-452`; `docs/LIMITATIONS.md`; `docs/CANDIDATES.md:94-98`; `SECURITY.md` | **A-02 is disclosed only in `SECURITY_MODEL.md`.** The technical report lists "a per-run restricted PostgreSQL role" as a control with no caveat; it also describes v0.4.0, not the hardening. | grep: superuser/monitor mentions are 0 in README, LIMITATIONS, SECURITY.md | A reviewer reading the report could take the role as a complete database boundary | Add one sentence on A-02 to the technical report §9, README limitations, LIMITATIONS and CANDIDATES | **Should fix before sending** |
| FR-5 | Medium (process) | git state | **The hardening is uncommitted and unpublished.** GitHub `main` is v0.4.0 (A-01 present; less complete security documentation). | `git status`; HEAD = `v0.4.0` | A reviewer of the GitHub repository sees the weaker version | Commit the branch (e.g. v0.4.1) after the doc fixes; decide what to share | **Yes, for sharing the hardened version** |
| FR-6 | Medium | `backend/tests/conftest.py:15-26`; `tests/test_integration_pg.py:46,71`; `tests/test_differential_pg.py:64` | **Transient infrastructure failure in one full-suite run, cause not recorded.** One cached run of S02 × m1 × seed 1 ended `infra_error` during an abnormal 88-minute run (network outage reported); the session-wide `run_cache` reused it in 3 tests, and no assertion prints `infra_error`. | §5: not reproduced (modules 21/21; 5/5 fresh runs with the frozen hashes; second full run) | Wall-clock safety limits (20 s step, 10 s connect, 15 s monitor lock) can fire when the machine stalls or sleeps; the cause is invisible in the report | Print `run["infra_error"]` in those assertions; do not cache runs that ended with an infrastructure error | No |
| FR-7 | Low | `docs/CANDIDATES.md:94` | "The actor connects to PostgreSQL as a per-run role" is stale: the harness connects as that role on the actor's behalf (A-01). | Read vs `actor/connection.py` | Misdescribes the boundary | Reword | Should fix before sending |
| FR-8 | Low | `README.md:349` | Test count says 464 / 73 PostgreSQL; the suite now has 475 / 81. | `pytest --co` | Stale number | Update | Should fix before sending |
| FR-9 | Low | `tests/test_audit_hardening_pg.py:228,332,361`; `tests/test_candidate_pg.py:98`; `tests/test_candidate_sandbox.py:114` | Wall-clock thresholds (`< 10 s`, `< 15 s`, `< 40 s`) can flake on a loaded or suspended machine (cells took up to 85 s under load in the performance study). | Performance investigation | Flaky failures, not false passes | Loosen to generous bounds or measure CPU-independent signals | No |
| FR-10 | Low | `tests/test_audit_hardening.py:388`; `tests/test_audit_hardening_pg.py:107,116-135,258` | Weak spots: the simulated concurrent-cleanup test does not discriminate (the real Job-handle test does); the in-process probe has no in-suite positive control (it was validated once against v0.4.0, where it found 77 driver modules and a live connection); the DSN check is a substring heuristic; the equivalence list never returns a JSONB row (the 180-cell comparison covers JSONB indirectly); a watchdog abort during an in-flight statement is untested. | Code read | Some protections are less directly tested than they appear | Add a positive control for the probe, a JSONB row, and an abort-mid-statement test | No |
| FR-11 | Low | `backend/gauntlet/actor/connection.py` (`_StatementCursor._stream_fetchone_gen`) | Overrides a private psycopg method. Diffed against psycopg 3.3.6: differs only by recording the final status/row count, describing empty results and not raising for statements without rows; `stream()` forces the extended protocol and cancels on early exit. Pinned in `backend/constraints.txt`. | Source diff (§4); equivalence test | A psycopg upgrade could break it silently except for the tests | Re-run the equivalence tests on any psycopg upgrade | No |
| FR-12 | Low | `backend/gauntlet/actor/child.py` (`classify`) | Classification uses the statement's first keyword: a `SELECT` calling a function that writes is a "read" (multi-statement evasion is closed). | Code read | A `before_write` fault point can be dodged by a writing function call | Document; consider classifying function calls conservatively | No |
| FR-13 | Low | repository | The 180-cell comparison script used for the evidence claim is not in the repository (it is an audit scratch script); POSIX/Linux paths are unverified. | — | A reviewer cannot reproduce the 180/180 claim with a repository command | Add the comparison script (e.g. `backend/scripts/compare_frozen_evidence.py`) and a Linux CI job | No |
| FR-14 | Info | — | Performance: about +0.18 ms per statement (≈3% of a run) from the extended protocol and per-row accounting; no regression measurable above this machine's load noise. | Interleaved A/B study (§5) | Small | None needed | No |
| FR-15 | Info | `candidates/loader.py` | Harness-owned methods (`app_register`, `app_deregister`) execute inside the candidate process and could be patched at runtime; their SQL is still gated and logged. | Code read | Integrity of instance registration not guaranteed | Move registration into the harness | No |
| FR-16 | Info | `config.py:14-17`; `api/app.py` | Default superuser password and an unauthenticated local API (earlier findings F-02, F-03) remain. | Earlier audit | Local-only risk | Random credentials at `pg init`; host-header allow-list | No |

## 4. Security: demonstrated, best-effort, limitations, untested

**Demonstrated (tests execute and fail on the old code or include a positive control):**

* The candidate's actor process holds no database connection, no DSN, no credentials and no loaded driver;
  verified from inside a sandboxed candidate process (`test_candidate_process_holds_no_database_connection_or_driver`).
* The former `db._conn` bypass fails and writes nothing (`test_former_bypass_attempt_fails_and_nothing_reaches_the_database`).
* Every statement executed on an actor connection is in the run's `statement_log`, and every client
  statement is in the hashed trace (`test_every_statement_the_harness_executes_for_a_candidate_is_traced`).
* One request is one SQL statement: multi-statement strings are rejected by the server (`42601`) with no
  effect, with a positive control showing the simple protocol would run them.
* Row and byte budgets stop a 50-million-row result early and cancel it; unrepresentable values are refused;
  the session stays usable.
* Statement class and transaction state come from the harness, not the actor; forged and malformed protocol
  messages are failures, never passes.
* Cleanup continues after failures (databases, roles and execution slots are released); the Windows Job
  handle is closed exactly once under concurrency; snapshots fail promptly instead of hanging on a lock.
* The trusted-environment gate refuses candidates without the declaration; `strict` mode fails closed.
* The per-run role cannot connect to another run's database (with a positive control).

**Best-effort (defense in depth, bypassable):** the Python audit-hook guard (writes, processes, sockets,
ctypes, driver imports); static candidate validation; the allow-listed environment against native code.

**Known limitations:** A-02 (FR-1); `process` mode is not a boundary (FR-2); host-file reads including the
oracle; candidate-caused timeouts misattributed (FR-3); first-keyword classification (FR-12); default
credentials and the unauthenticated API (FR-16).

**Untested assumptions:** POSIX/Linux behaviour (RLIMITs, no process-count limit); the watchdog cancel path
with a statement in flight; behaviour with hostile native code (deliberately not executed); psycopg
versions other than 3.3.6.

## 5. Tests: commands, environment, results, reproducibility

Environment: Windows 11 (`Windows-11-10.0.26300-SP0`), Python 3.14.0, psycopg 3.3.6 (binary), libpq
180004, PostgreSQL 16.14 (portable cluster for this worktree on 127.0.0.1:55434, data under the worktree's
git-ignored `.gauntlet/`), the 31 dependency versions of `backend/constraints.txt`.

Command (from `backend/`):
`GAUNTLET_PG_DSN=postgresql://gauntlet:gauntlet@127.0.0.1:55434/postgres python -m pytest -q -p no:cacheprovider -rfEsxX`

| Run | Result | Duration | Notes |
|---|---|---|---|
| Full suite, run 1 (13:32–15:00) | **472 passed, 3 failed**, 0 skipped (475 collected; 81 need PostgreSQL) | 88 min (normal: 9–13 min) | Abnormal machine conditions (author reported a network outage). All 3 failures come from one cached run of S02 × m1 × seed 1 that ended `infra_error`; fresh runs inside the same tests produced the frozen hashes. Cause not recorded (FR-6). |
| Re-run of the two failing modules | **21 passed** | 72 s | `pytest tests/test_integration_pg.py tests/test_differential_pg.py` |
| The failing cell, 5 fresh executions | 5/5 `fail` with trace `02019389…`, outcome `74a63f00…` (the frozen values), no infrastructure error | ≈3.5 s each | |
| Full suite, run 2 (15:11–15:21) | **475 passed, 0 failed, 0 skipped** (81 need PostgreSQL) | 9 min 25 s | Quiet machine; same command and environment |

Reproducibility: the PostgreSQL tests need a reachable PostgreSQL 16 server (`GAUNTLET_PG_DSN`); without one
they skip. Job-object tests are Windows-only; POSIX was never run. The two "source changed since process
start" tests fail by design if harness files are edited during a run. Timing thresholds can flake under
heavy load or sleep (FR-9).

## 6. Frozen benchmark and evidence

* `(cd evidence && sha256sum -c *.sha256)`: 4/4 OK (also identical byte-for-byte to the release repository's copies).
* 180-cell comparison (15:00–15:09, after run 1): every cell of the frozen v0.3.0 benchmark
  `bench-eed0c47427` re-executed with the current code and compared with the raw run records in
  `evidence/results-bench-eed0c47427.sqlite3` (opened read-only, `mode=ro&immutable=1`): **180 identical,
  0 different** in schedule hash, trace hash, outcome hash and verdict; 540 s.
* `git diff --name-only v0.4.0 -- evidence …` (all protected paths): empty. Matching results were not obtained
  by changing benchmark inputs, oracle, scenarios, references, mutants or evidence.
* The comparison script is an audit scratch script, not part of the repository (FR-13).

## 7. Documentation accuracy

Accurate: README status, release history and capability table; `docs/SECURITY_MODEL.md` (A-01 resolution,
the residual new-connection path with the corrected credentials wording, A-02, default credentials);
`docs/ARCHITECTURE.md`; `docs/DEMO.md` (every command re-verified from a clean copy on 2026-11-04);
`docs/LIMITATIONS.md` on agents ("No solution produced by Claude, Gemini or any other model has been
evaluated"). No document claims an AI agent, Gemini or Google has been evaluated, and no document calls
`process` isolation a security boundary.

Inaccurate or incomplete: FR-4 (A-02 missing outside the security model; the technical report describes
v0.4.0), FR-7 (`CANDIDATES.md:94`), FR-8 (`README.md:349`).

## 8. External-review readiness

| Audience | Assessment |
|---|---|
| Technical reviewer reading code and report | Ready after FR-4/FR-7/FR-8 are fixed and the branch is committed (FR-5). The code is readable, the security model is candid, and the tests are specific. |
| Researcher evaluating the methodology | Ready with limitations: frozen, checksummed evidence that reproduces exactly; honest scope (hand-written references and mutants, synthetic fixtures, one task family, author-run). Independent reproduction and a Linux run are still missing (FR-13). |
| An external engineer, as a work sample | Suitable as a work sample, with the scope stated plainly: an instrument validated on hand-written implementations, **no AI agent evaluated**, no verified isolation boundary, author-run validation. |

External-review readiness is not production or security readiness.

## 9. Prioritized remaining actions

1. Fix the documentation gaps (FR-4, FR-7, FR-8), then commit the branch (e.g. v0.4.1) and decide what to share (FR-5).
2. Make test diagnostics show infrastructure errors and stop caching failed runs (FR-6); loosen wall-clock thresholds (FR-9).
3. Attribute candidate-caused statement timeouts to the candidate (FR-3).
4. Add the frozen-evidence comparison script to the repository and a Linux CI job (FR-13).
5. Strengthen the tests noted in FR-10.
6. Before any agent study: fix A-02 (FR-1) and build a verified `strict` boundary (FR-2).

## 10. Files changed during this audit

Only this report (`docs/FINAL_RELEASE_READINESS_AUDIT.md`, new) was written. No code, test, evidence or other
documentation file was modified. Temporary run artifacts (logs, PostgreSQL data) are outside the repository
or in git-ignored directories.

## 11. Documentation follow-up (2026-11-05)

After the audit, documentation-only edits were made to `README.md`, `docs/CANDIDATES.md`,
`docs/LIMITATIONS.md`, `docs/SECURITY_MODEL.md`, `research/technical-report.md` and `SECURITY.md`. No
code, test, evidence, scenario, oracle, reference or mutant file changed. The findings above are unchanged
as a record of the audit.

| ID | Status after the follow-up |
|---|---|
| FR-4 | **Documentation addressed.** A-02 is now disclosed in the README limitations, `docs/LIMITATIONS.md`, `docs/CANDIDATES.md`, the technical report §9 and `SECURITY.md`. The technical report §9 labels the harness-owned connection as unreleased audit hardening. |
| FR-7 | **Addressed.** `docs/CANDIDATES.md` now states that the harness owns the database connection and mediates candidate SQL through the request channel, and that this is not complete database isolation. |
| FR-8 | **Addressed.** `README.md` now gives 475 passed, 0 failed, 0 skipped (81 need PostgreSQL), author-run on Windows; the v0.4.0 figures (409 / 67) are kept as historical. |

**Still unresolved in the implementation:** A-02 (FR-1: the monitor still reads the candidate's database
as a PostgreSQL superuser) and the timeout classification (FR-3: a candidate statement that exceeds the
step timeout is still classified `infrastructure`). Only their disclosure changed. FR-2 (`process` mode
is not a security boundary) and all other findings are also unchanged.

## 12. FR-3 follow-up (2026-11-05)

FR-3 is addressed in the implementation by a later, separate commit. When a statement exceeds the step
timeout, the harness records evidence and then ends the statement and the actor. It classifies an external
candidate's run `candidate_statement_timeout` only when all of these hold:

* the harness's wait loop did not stall;
* the server reported the statement active for the whole step, on CPU or in `pg_sleep`;
* a cancel request ended it.

Every other case, and every built-in implementation, stays `infrastructure`. SQLSTATE 57014 alone is never
sufficient. The rule is a heuristic (see `docs/SECURITY_MODEL.md`). A-02 (FR-1) remains unresolved.

## 13. A-02 follow-up (2026-11-05)

A-02 (FR-1) is addressed in the implementation by a further commit, for external candidates:

* snapshots are read by a per-run non-superuser observer role (`gauntlet_obs_*`), whose read access the
  candidate role grants before any candidate code runs;
* the observer's `search_path` is pinned, `row_security` is off, queries are schema-qualified, and the
  observed tables must be plain tables;
* lock probes, liveness, backend termination and statement activity use a control connection to the
  maintenance database;
* no superuser session opens in the run database (tested, with a positive control for the escalation path:
  `backend/tests/test_observer_role_pg.py`);
* obstruction of observation is a `candidate_execution_failure`, never a pass.

Still open: superuser credentials remain in the harness (provisioning, control connection), and native
candidate code can read them in `process` mode (FR-2). Built-in implementations are still observed as
superuser. The non-superuser-admin configuration is untested.
