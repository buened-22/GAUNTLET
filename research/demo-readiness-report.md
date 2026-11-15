# Demo readiness report

| | |
|---|---|
| Build | branch `gauntlet/full-audit-hardening`, commit `2a73551` (unreleased audit hardening on `v0.4.0` = `be9b742`) |
| Date | 2026-11-05 |
| Environment | Windows 11, Python 3.14.0, psycopg 3.3.6, PostgreSQL 16.14 (portable local cluster) |
| Who | Author-run. Nothing here has been independently reproduced or reviewed. |
| Verdict | **Ready for a technical demo** of the synthetic proof of concept, with the limitations below. **Not ready** to run untrusted or AI-generated code. |

Labels: **[V]** verified on the build above (command run, output observed); **[R]** recorded evidence from an
earlier run, not re-run here; **[A]** assumption or recommendation.

## 1. Demo rehearsal at `2a73551`

Every live and pre-session step of [demo-script.md](demo-script.md) was run on 2026-11-05 from `backend/`,
against a scratch copy of the v0.3.0 evidence database and a scratch candidates directory (both under the
git-ignored `.gauntlet/`). The working tree was unchanged afterwards. Results **[V]**:

| Step | Command | Observed | Time |
|---|---|---|---|
| Server | `python -m gauntlet pg check` | `"is_pg16": true` | 1 s |
| Evidence | `(cd evidence && sha256sum -c *.sha256)` | 4 × `OK` | <1 s |
| Differential | `diff --impls ref_a_trigger,m1_stale_backfill --scenario S02_… --seed 1` | `CONFIRMED_VIOLATION`; m1 fails `S7_LINEARIZABLE`, `S1_ACKED_DURABLE`; reference passes | 11 s |
| Differential | `diff --impls ref_a_trigger,ref_b_dualwrite …` | `AGREEMENT` | 9 s |
| Exploration | `explore --scenario F01_backfill_race_probe --impl m1_stale_backfill` | 18 executed, 13 pass / 5 fail, exhaustive within bounds, 0 unexplored, one failure class `c554f673482f`, replay reproduced | 54 s |
| Minimization | `minimize <m1 run> --invariant S1_ACKED_DURABLE` | 1-minimal; directives 104 → 19, operations 54 → 3, trace events 390 → 51; replay signature match | 47 s |
| Replay | `replay <m1 run>` | schedule `93b9e5c8f3edbed7`, trace `02019389e4b0f7c1`, outcome `74a63f00c8ed4b68`, verdict `fail`, all `same` | 5 s |
| Isolation | `candidate isolation` | strict unavailable; process available, `security_boundary: false` | 1 s |
| Snapshot | `candidate validate` / `register tests/candidates/synthetic_stale_backfill` | `ok: true`; `cand-6478453bb5dd4e78` | 1 s each |
| Fail-closed | `candidate evaluate … --isolation process` (no trust declaration) | `BLOCKED: IsolationUnavailable …`, exit 3 | 1 s |
| Fail-closed | `candidate evaluate … --isolation strict` | `BLOCKED: … strict isolation unavailable …`, exit 3 | 2 s |
| Evaluation | `candidate evaluate … --trusted-dev-env --scenarios S01…,S02… --seeds 1 --refs ref_a_trigger --explore-max-schedules 18 --minimize-budget 20` | completed; S01 `agreement`; S02 `confirmed_violation` (S7, S1); 0 runs without a verdict; exploration 18 (13/5); minimization 104 → 19; experiment report evidence 5/5 unchanged | 152 s |
| Snapshot check | `candidate verify cand-6478453bb5dd4e78` | `ok: … matches its recorded source hash` | 1 s |
| Audit tests | `pytest -q tests/test_observer_role_pg.py tests/test_step_timeout.py tests/test_step_timeout_pg.py` | `36 passed` (plus one pre-existing `PytestUnraisableExceptionWarning`) | 79 s |
| API evidence check | `python -m gauntlet serve --port 8765` on the rehearsal's scratch database, then `curl http://127.0.0.1:8765/api/experiments/expe-52071be9bd44/verify` (the evaluation's experiment report) | `{"ok":true,"checked":5,"missing":[],"modified":[]}`; an unknown id returns HTTP 404; server stopped afterwards | <1 s |

The replay hashes equal those recorded for the same run in the frozen evidence
(`evidence/results-bench-eed0c47427.sqlite3`, m1 × S02 × seed 1, read in immutable mode; checksum unchanged
afterwards) **[V]**, and equal the v0.4.0 capture in [demo-transcript.md](demo-transcript.md) **[V]**.

**Not rehearsed at `2a73551`:** the dashboard (`npm run dev` against `python -m gauntlet serve`). The
frontend is unchanged since `v0.4.0`; `npm run typecheck` and `npm run build` last passed on 2026-11-04
**[R]**. **[A]** Show the dashboard only if rehearsed beforehand. The 5-minute script does not use it.

## 2. Verified capabilities and their evidence

| Capability | Evidence | Label |
|---|---|---|
| Statement-level gating, fault injection, deterministic replay | Replay hashes identical (§1); 60/60 reproducibility re-runs in the frozen benchmark | [V] / [R] |
| Independent oracle (12 invariants) separating correct from defective code | Frozen v0.3.0: 36/36 reference runs pass, 8/8 mutants killed by their intended invariant ([results-summary.md](results-summary.md)) | [R] |
| Unchanged behaviour after the audit hardening | All 180 frozen cells re-executed: 180/180 identical schedule, trace and outcome hashes and verdicts (2026-11-05, after the A-02 change) | [V] |
| Differential classification | `CONFIRMED_VIOLATION` and `AGREEMENT` (§1) | [V] |
| Bounded exhaustive exploration | 18 schedules, one failure class, replay reproduced (§1) | [V] |
| Minimization with preserved signature | 104 → 19 directives, 1-minimal, signature match (§1) | [V] |
| External-candidate pipeline with content-addressed snapshots | validate / register / verify (§1) | [V] |
| Fail-closed candidate execution | process without trust declaration and strict both blocked, exit 3 (§1) | [V] |
| Experiment reports with hashed evidence | evidence-manifest verification 5/5 (§1) | [V] |
| A-01: harness-owned connections, one statement per request, bounded results | `backend/tests/test_audit_hardening_pg.py` in the full suite | [V] |
| FR-3: evidence-based step-timeout classification | `backend/tests/test_step_timeout*.py` (§1, and full suite) | [V] |
| A-02: non-superuser observer, no superuser session in a candidate's run database, escalation path closed (with positive control) | `backend/tests/test_observer_role_pg.py` (§1, and full suite) | [V] |

## 3. Test, benchmark and checksum status

| Check | Result | Label |
|---|---|---|
| Full suite (`cd backend && python -m pytest -q`), 511 tests, 96 need PostgreSQL | **510 passed, 1 failed, 0 skipped** (26 min, under heavy disk load) | [V] |
| The failure | `test_audit_hardening_pg.py::test_row_and_byte_budgets_stop_the_statement_without_buffering_it`: its `< 10 s` wall-clock bound took 11–36 s because PostgreSQL materializes `generate_series(1, 50000000)` to disk while OneDrive sync loaded the disk. It fails the same way on the previous commit under that load; it is finding FR-9 (timing-sensitive thresholds). The test was not changed. | [V] |
| An earlier full run under worse load | 508 passed, 3 failed (different timing-related tests); each passed when re-run alone | [V] |
| Previous commit (`58f628e`) full run | 502 passed, 0 failed | [V] |
| Frozen benchmark re-execution | 180/180 identical | [V] |
| Evidence checksums | 4/4 OK (also re-checked during the rehearsal) | [V] |

## 4. Demo prerequisites

* Python ≥ 3.11 with the backend installed (`pip install -c backend/constraints.txt -e "backend[dev]"`),
  Node ≥ 18 only for the portable PostgreSQL download and the dashboard ([../docs/DEMO.md](../docs/DEMO.md) step 1).
* PostgreSQL 16 running (`python -m gauntlet pg start`; `pg check` reports `"is_pg16": true`).
* `GAUNTLET_RESULTS_DB` and `GAUNTLET_CANDIDATES_DIR` pointing at scratch locations; never at `evidence/`.
* The pre-session steps of [demo-script.md](demo-script.md) completed beforehand: about 7 minutes of execution
  (diff 11 s, exploration 54 s, minimization 47 s, trusted evaluation 152 s, audit tests 79 s, measured in §1).
  The 5-minute live part then executes only `diff` (11 s), `replay` (5 s) and the two blocked evaluations
  (1–2 s each).
* **[A]** No network access is needed during the demo. Run it on a machine you treat as disposable only if you
  evaluate anything beyond the repository's own fixtures; for the scripted demo, all executed code is in the
  repository.
* **[A]** Pause OneDrive or other heavy disk activity: it slowed runs severalfold during validation.

## 5. Known risks and limitations

| Risk | Impact on the demo | Mitigation |
|---|---|---|
| Disk or CPU load slows runs (observed: suite 10 → 26–66 min) | Live steps take longer | Pre-run slow steps; fall back to [demo-transcript.md](demo-transcript.md) |
| Run ids differ per execution | Must copy the mutant's run id from the `diff` output for `minimize` / `replay` | Script says "note the run id" |
| `PytestUnraisableExceptionWarning` in the audit-test step | Noise in the output | Explain it is pre-existing and harmless |
| At commit `2a73551` (the rehearsed build) the candidate summary's isolation lists are incomplete. "Applied" omits the A-01 harness-owned connections and the A-02 observer role; "Not enforced" omits the harness's environment and memory and a new database connection opened by native code. A later change on this branch (after `2a73551`) to `backend/gauntlet/candidates/sandbox.py` (`check()`) corrects the record, with tests in `test_candidate_sandbox.py` and `test_candidate_pg.py`. The rehearsal summary predates that change | A summary generated at `2a73551` understates both the controls and the gaps | Regenerate the summary after the change, or describe both from [../docs/DEMO_SECURITY_NOTES.md](../docs/DEMO_SECURITY_NOTES.md) |
| The API is unauthenticated | Only if the dashboard is shown | Bind to `127.0.0.1` only (default) |

Limitations to state (details: [../docs/LIMITATIONS.md](../docs/LIMITATIONS.md),
[../docs/DEMO_SECURITY_NOTES.md](../docs/DEMO_SECURITY_NOTES.md)):

* **`process` isolation is not a security boundary**; `strict` is not implemented. Untrusted or AI-generated
  code must not be run outside a disconnected, disposable VM.
* No study of an AI coding agent has been run. One Gemini-generated candidate was evaluated once, inside a
  disconnected VM, after this report's rehearsal ([gemini-attempt2-results.md](gemini-attempt2-results.md)).
* One task family, PostgreSQL 16, statement-level granularity, bounded exploration.
* All validation is author-run on one Windows machine; nothing is independently reproduced.

## 6. Ready versus not yet validated

| Ready to show | Not yet validated / not to claim |
|---|---|
| Differential, exploration, minimization, replay, API evidence check (rehearsed) | Dashboard at `2a73551` (not rehearsed) |
| Candidate pipeline with synthetic fixtures, fail-closed gates (rehearsed); one Gemini candidate evaluated in a disconnected VM ([gemini-attempt2-results.md](gemini-attempt2-results.md)) | Any study of an AI agent (more attempts, seeds, a pre-registered plan) |
| Audit fixes A-01, FR-3, A-02 with their tests (rehearsed) | A security boundary for untrusted code; independent security review |
| Frozen benchmark reproduction 180/180, checksums 4/4 | A clean full-suite run on this commit (latest: 510/1, FR-9) |
| | POSIX sandbox path, Docker Compose path, non-superuser admin configuration |
