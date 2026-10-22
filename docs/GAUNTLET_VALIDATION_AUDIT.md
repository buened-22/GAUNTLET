# Gauntlet v0.1.0: validation audit

**Status: self-audit by the system's author, not independent validation.** No code, tests, scenarios
or thresholds were changed during this audit. The audit scripts were run from a temporary folder outside
the repository. Byte-identical copies are now stored in `audit/` (see `audit/PROVENANCE.md`). Each finding
is labelled **[VERIFIED]** (an observed command output) or **[INTERPRETATION]**
(my reading of that output).

Environment: Windows 11, Python 3.14.0, pytest 9.1.1, PostgreSQL 16.14 (portable local cluster on port
55432), audit date 2026-10-18.

---

## Prioritized findings

| # | Severity | Finding | Type |
|---|---|---|---|
| 1 | **High** | The oracle identifies a logical operation by the harness's `logical_id`, **not** by its idempotency key, contrary to `docs/INVARIANTS.md`. If a key is reused under a new logical id, a correct idempotent implementation is reported FAIL and a duplicating implementation PASSES. | VERIFIED |
| 2 | **High** | Seeds barely vary the schedules. In S01, S03, S04 and S05 the directive structure **and** the target order ids are identical for seeds 1–3; only the amounts change. "Detected in 3/3 runs" is mostly three replicas of one schedule. | VERIFIED |
| 3 | **Medium** | The S1 diagnosis over-attributes. The `lost_ack` relaxation lets the state jump to any previously held value, so S1 is reported FAIL in histories where no acknowledged write was lost. | VERIFIED |
| 4 | **Medium** | The design is circular: the same author wrote the scenarios, the mutants, the oracle contracts and the conventional suite. Each mutant's intended scenario contains the schedule built to trigger it, and the conventional suite was written *after* the mutants and their adversarial results were known. | VERIFIED (chronology from the session) / INTERPRETATION (bias impact) |
| 5 | **Medium** | Three of the five "passes conventional, fails adversarial" mutants (m3, m7, m8) are caught by simple sequential, fault-free schedules. Only m1 and m2 were not exposed by any sequential schedule tried. | VERIFIED |
| 6 | **Low** | Some intended invariants are non-specific. R4 (m5) fails for any incomplete migration; S7-unexplained (m7) is a catch-all. "Killed as intended" for these says less than it appears to. | INTERPRETATION |
| 7 | **Low** | Reproducibility is mostly guaranteed by construction (a single-threaded controller), and every re-run used seed 1, back-to-back, on one server. | VERIFIED (setup) / INTERPRETATION |
| 8 | **Low** | Of the "14 PostgreSQL integration tests", 1 never contacts the server (the unreachable-DSN test) and 1 only checks the version. **12** exercise the system against PostgreSQL. | VERIFIED |
| 9 | **Low** | One documented limitation is overstated. `LIMITATIONS.md` says uncertain effects may be placed anywhere in the union of attempt windows; the audit shows effects in the *gap* between attempts are correctly rejected (case UN7). | VERIFIED |

---

## 1. Test suite: commands and outputs [VERIFIED]

```
cd backend
..\.venv\Scripts\python -m gauntlet pg check
..\.venv\Scripts\python -m pytest -v -p no:cacheprovider
..\.venv\Scripts\python -m pytest -m pg -v -p no:cacheprovider
```

Results:

* Full suite: `75 passed, 1 warning in 56.30s` (the warning is a Starlette deprecation notice).
* `-m pg` subset: `14 passed, 61 deselected in 52.01s`.

The 14 PostgreSQL-marked tests:

| Test | Touches PostgreSQL? |
|---|---|
| test_api_store_report::test_run_lifecycle_through_api | yes (2 full runs through the API) |
| test_api_store_report::test_run_persistence_roundtrip_and_report | yes |
| test_integration_pg::test_server_is_postgres_16 | version query only |
| test_references_pass_baseline[ref_a_trigger] / [ref_b_dualwrite] | yes |
| test_reference_a_survives_crash_and_resume | yes |
| test_stale_backfill_mutant_killed_by_intended_invariant | yes |
| test_nonatomic_dual_write_mutant_killed_by_intended_invariant | yes |
| test_retry_duplication_mutant_diagnosed_precisely | yes |
| test_same_seed_reproduces_schedule_trace_and_outcome | yes |
| test_unreachable_database_is_infra_error_not_pass | **no**: connects to port 1 by design |
| test_pause_before_write_and_lock_wait_detection | yes |
| test_crash_before_commit_rolls_back_and_after_commit_persists | yes |
| test_lost_response_then_idempotent_retry | yes |

## 2. What the 180 runs are [VERIFIED]

* **One run** is a single execution of (scenario, implementation, seed). It gets a fresh PostgreSQL
  database with the harness-owned legacy schema, one OS process per actor incarnation, the scenario's
  directives executed statement by statement, a settle phase, a final snapshot, the oracle, and
  persistence.
* **Composition.** 10 implementations × 6 scenarios × seeds {1, 2, 3} = 180 distinct cells, each
  present exactly once. That is 30 runs per scenario and 18 per implementation.
* **Additional runs.** 60 reproducibility re-runs (one per implementation × scenario, seed 1), tagged
  with the same benchmark id, giving 240 rows in total.
* **Runs outside the benchmark.** Two API demonstration runs: S03 with seed 11, `ref_b_dualwrite`
  passed and `m2_nonatomic_dual_write` failed.
* **Seed selection.** Seeds were the CLI default `[1, 2, 3]`, fixed before the benchmark and not chosen
  by outcome. However, seed 1 was used throughout development (see §7), and seeds change little (see
  Finding 2).

Per-scenario seed sensitivity, from `schedule_definition`:

| Scenario | Directive structure identical across seeds | Op targets identical | Seeded random interleave |
|---|---|---|---|
| S01 | yes | yes | 0 |
| S02 | yes | no | 1 |
| S03 | yes | yes | 0 |
| S04 | yes | yes | 0 |
| S05 | yes | yes | 0 |
| S06 | yes | no | 1 |

## 3. References pass; mutants fail for their intended invariant [VERIFIED]

Read from each run's stored oracle output, not from summary tables.

* `ref_a_trigger`: 18/18 runs pass; invariant results are 171 pass and 45 not_applicable.
* `ref_b_dualwrite`: identical counts.
* The not_applicable results are R1, R2 and R3 in the 15 non-S04 runs. **Recovery invariants are
  exercised for the references in only 3 runs each (S04).**

| Mutant | Intended invariant | Runs (scenario, seed: run id) | Intended status | Other FAILs |
|---|---|---|---|---|
| m1 | S1 | S02 s1 `165ca695d02c49e5`, s2 `40b8d6d552994ff9`, s3 `b3116b252ed346e9` | fail ×3 | S7 |
| m2 | S3 | S03 s1 `6e694b9654e54b83`, s2 `3297abc493a74255`, s3 `145df5e8229f4bd6` | fail ×3 | S7, S1 |
| m3 | S4 | S03 s1 `237b8b415a04445e`, s2 `7b7aa398e8294656`, s3 `d26ab80871af4007` | fail ×3 | S7 |
| m4 | C1 | S05 s1 `c190f57a2b6442ac`, s2 `c170b3171a2146fe`, s3 `4eae50806175404a` | fail ×3 | – |
| m5 | R4 | S01 ×3 (`0e58435e…`, `f883026e…`, `d34f03c5…`), S04 ×3 (`f5fe4a31…`, `0f9c3a46…`, `21c9fcac…`) | fail ×6 | R1, R3 (S04) |
| m6 | S2 | S06 s1 `19c71354daea46eb`, s2 `2dcc269efb0d4987`, s3 `8e00d5dba08345eb` | fail ×3 | S7 |
| m7 | S7 | S06 s1 `e0e914dbda554cd2`, s2 `c7641436c9a74992`, s3 `a73a22f97336441f` | fail ×3 | – |
| m8 | S1 | S01 s1 `16bbf9e9da824ee3`, s2 `1f9e9ef0e63e44fc`, s3 `115cab35427c405d` | fail ×3 | S7, S3 |

No run had an infrastructure error. The trigger sequence numbers are identical across seeds (for
example, 76 for every m1 run), which is consistent with Finding 2.

## 4. The five mutants that pass the conventional suite

**Conventional tests executed [VERIFIED].** All ten tests, CT01–CT10, ran and passed for m1, m2, m3, m7
and m8 (from the benchmark summary):

* CT01: v1 create/read
* CT02: v1 charge
* CT03: v1 repeated key
* CT04: not_found
* CT05: v2 reads legacy rows
* CT06: v2 create/set/charge/read
* CT07: full sequential migration
* CT08: finalize refused while v1 registered
* CT09: v2 after contract
* CT10: invalid amounts rejected

**Exposing adversarial traces [VERIFIED]** (seed-1 runs; excerpts from `audit_traces.py`):

* **m1** (S02, `165ca695d02c49e5`, order 2):
  * #24–28: the backfill reads totals and is **paused before its first write** (F3).
  * #29–35: v1 `set_total(2, 9.78)` commits and is acknowledged.
  * #44: the backfill resumes with `UPDATE … SET total_cents = 27265 WHERE id = 2`, a stale value.
  * #76: the snapshot shows `total = 272.65`. The acknowledged 9.78 is gone.
* **m2** (S03, `6e694b9654e54b83`, order 5), after finalize:
  * #143–145: transaction 1 updates `total` and stores the idempotency response (`81.86`), then
    commits.
  * #146–147: the process is **killed after its first commit** (F1).
  * #152–158: the retry on v2-b finds the key and replays `81.86` as success.
  * #182: a v2 read returns `73.57` from the stale `total_cents`.
* **m3** (S03, `237b8b415a04445e`, order 1):
  * #27–30: the charge commits (113.31) but the **response is lost**.
  * #31–37: the retry applies the charge again (120.03).
  * #90: a read confirms 120.03.
* **m7** (S06, `e0e914dbda554cd2`, order 2):
  * #31: v2 runs `COALESCE(total_cents, 0) + 3698` on an unbackfilled row, and the result is
    acknowledged as 36.98. The initial value was 122.47.
  * #68: v1 reads 36.98.
* **m8** (S01, `16bbf9e9da824ee3`, order 2):
  * #105–111: after the backfill, v1 sets 438.53.
  * #129: finalize skips reconciliation.
  * #138: the snapshot shows `total = 438.53` but the authoritative `total_cents = 27565`.

**Classification: tested, not asserted [VERIFIED].** These are sequential, fault-free schedules: no
`run_until`, `crash`, `lose_response`, `interleave` or `restart`. Each was run against the mutant and
its reference.

| Mutant | Sequential schedule | Reference | Mutant | Class |
|---|---|---|---|---|
| m1 | legacy write before the backfill, backfill to completion, legacy write after | pass | **pass** | Requires adversarial scheduling |
| m2 | finalized-stage charge, then a sequential replay of the same logical op | pass | **pass** | Requires fault injection (crash between transactions) |
| m3 | v2 charge, then a sequential replay of the same logical op | pass | **fail** (S7, S4) | Conventional coverage gap |
| m7 | v2 charge on an unbackfilled row | pass | **fail** (S7) | Conventional coverage gap |
| m8 | legacy write after the backfill, drain, finalize, v2 read | pass | **fail** (S7, S1, S3) | Conventional coverage gap |

**[INTERPRETATION]** The m1 and m2 results do not prove that *no* sequential schedule could expose them.
The mechanisms do argue for it, though:

* m1 needs a commit *between two statements of one backfill transaction*, which cannot happen when
  operations run to completion one at a time.
* m2 needs the process to stop between its two transactions; without a fault, transaction 2 always
  runs.

## 5. Oracle audit: constructed histories [VERIFIED outputs]

Source: `audit_oracle.py`. Initial value 10.00. An attempt invoked at seq i and completed, crashed or
lost at seq c may take effect only in (i, c).

| Case | Expected by the model | S7 | S1 | S4 | S6 |
|---|---|---|---|---|---|
| LW1 acked set never visible | FAIL; S1 | fail | fail | inc | inc |
| LW2 acked set read back, then reverted | FAIL; S1 | fail | fail | inc | inc |
| LW3 acked set persists | PASS | pass | pass | pass | pass |
| DU1 lost response + retry, applied once | PASS | pass | pass | pass | pass |
| DU2 lost response + retry, applied twice | FAIL; S4 | fail | inc | fail | inc |
| **DU3 key reused under a new logical id, applied once** | PASS per the docs | **fail** | fail | inc | inc |
| AC1 acked write, later op crashes, acked effect missing | FAIL | fail | fail | inc | inc |
| AC2 / AC3 acked write, later crashed op applied or not | PASS | pass | pass | pass | pass |
| UN1 / UN2 crashed after commit, never retried, applied or not | PASS | pass | pass | pass | pass |
| UN3 crashed, value matches neither outcome | FAIL | fail | inc | inc | inc |
| UN4 crashed, retry acked, effect absent | FAIL | fail | fail | inc | inc |
| UN5 effect appears only after the crash point | FAIL | fail | **fail** | inc | inc |
| UN6 uncertain effect observed, then disappears | FAIL | fail | **fail** | inc | inc |
| UN7 effect in the gap between attempts | FAIL | fail | **fail** | inc | inc |
| UN8 effect inside the retry's window | PASS | pass | pass | pass | pass |
| FA1 explicit failure that applied | FAIL; S6 | fail | inc | inc | fail |
| CO1 overlapping acked sets, either order | PASS | pass | pass | pass | pass |
| CO2 sequential acked sets, earlier value final | FAIL | fail | fail | inc | inc |

### Permitted outcomes, and why

* **Acknowledged write.** It must take effect exactly once, inside its attempt window, and stay
  visible until a later write supersedes it. *Why:* the client was told it committed.
* **Uncertain write** (crash or lost response). It may take effect **at most once**, and only inside
  a window during which some attempt was executing, between invocation and the crash or loss point.
  Once observed, it cannot disappear (UN6). It cannot appear after its process died (UN5). *Why:* the
  client cannot know whether the commit happened, but a dead process cannot commit later, and a
  committed effect cannot roll back.
* **Uncertain followed by an acknowledged retry** (same logical id). Exactly once, in either
  attempt's window (DU1, UN8). Not absent (UN4) and not twice (DU2).
* **Failed write.** It must have no effect (FA1).
* **Concurrent writes.** Any order consistent with real time (CO1, CO2).

### Discrepancies

* **DU3 (Finding 1).** `oracle/checker.py` groups write attempts by `a["logical_id"]`, but
  `docs/INVARIANTS.md` says "All attempts that share an idempotency key form one logical write".
  The benchmark is not affected: all scenarios use `key = "key-" + logical_id`, and retries reuse
  the logical id. External harnesses or agents could be affected. The audit first hit this in
  `audit_sequential.py`, where m2_seq and m3_seq showed the inverted verdicts.
* **UN5, UN6, UN7 (Finding 3).** S1 is attributed even though no acknowledged write was lost. The
  revert relaxation lets the state jump to *any* previously held value. The S7 verdicts are correct;
  only the diagnosis is broad.

## 6. Do the 60/60 re-runs compare meaningful content? [VERIFIED]

* **Trace hash input.** Every trace event except `t_ms`, `blockers` and `backend_pid`. That includes
  SQL text, parameters, row counts, statement errors, invocations, outcomes and fault and recovery
  events.
* **Outcome hash input.** Every attempt's status, reason, result and error; every invariant's status;
  the final snapshot rows.
* **Direct comparison of all 60 pairs.** Full logical event lists equal 60/60. All snapshot rows,
  including intermediate snapshots, equal 60/60. Attempts equal 60/60. Invariant
  (status, summary, evidence, trigger) equal 60/60. Wall-clock timings **differ** in 60/60, so the
  runs were really re-executed.
* **Content of the pairs.** On average 268 events and 168 granted statements per run. 8 pairs contain
  lock waits, 20 contain process kills, 20 contain seeded random interleavings.
* **Sensitivity.** Changing one statement parameter changes the trace hash; changing only timing does
  not.
* **Gap.** Intermediate snapshot rows are not inside the trace hash, but they were compared directly.
* **[INTERPRETATION]** Identity is expected by design, because one controller thread serialises every
  statement. The result shows that no nondeterminism leaked in (from PostgreSQL row order, OS
  scheduling or timing). It is *not* evidence of reproducibility across machines, PostgreSQL builds or
  seeds other than 1.

## 7. Shared assumptions that could inflate apparent accuracy

1. **One author for everything [VERIFIED].** Scenarios, references, mutants, oracle, strategy
   contracts (S3) and conventional suite were written by the same author in one session.
2. **Scenarios designed around mutants [VERIFIED by inspection].** S02's `run_until(before_write)` is
   exactly the m1 window. S03's post-finalize crash-and-retry is exactly the m2 window. S05 keeps v1
   alive after contract, exactly the m4 window. Mutant metadata names these as "intended scenarios",
   so "intended-scenario detection" is close to tautological. The random interleaving segments
   (S02, S06) are the only mutant-agnostic adversarial pressure.
3. **Conventional suite written after the mutants were known [VERIFIED chronology].** It was written
   after the seed-1 adversarial matrix had been run on all mutants. CT03 tests key replay on v1 only;
   m3 mutates v2. Intent cannot be audited, so this is a risk, not a demonstrated bias.
4. **Seed replication (Finding 2).** It inflates "3/3" figures.
5. **Same code path for integration tests and the benchmark [VERIFIED].** The PostgreSQL tests call
   the same `execute_run` and `RunOracle`, and assert the same intended invariants on the same seed-1
   schedules (through `run_cache`). They are consistency checks, not independent evidence.
6. **Oracle changed during development [VERIFIED chronology].** The `lost_ack` relaxation was
   broadened after a unit test failed, before the benchmark ran. The development transcript shows m1
   (S02) and m8 (S01) already diagnosed as S1 under the earlier relaxation, so the intended-kill
   results do not depend on the change. That earlier output was not re-produced during this audit.
7. **Strategy contracts encoded to match the references [INTERPRETATION].** S3's per-strategy rules
   (A: agree after expand; B: agree after finalize) mirror how the references were designed. An
   implementation with a different but valid protocol might be judged incorrectly.
8. **Recovery coverage is thin [VERIFIED].** R1, R2 and R3 apply in S04 only: 3 runs per
   implementation.

## 8. Unresolved issues (not fixed, per audit rules)

1. Logical-operation identity: key versus `logical_id` (Finding 1). Decide on the semantics, then fix
   either the code or the documentation.
2. Seeds should vary the structure of the schedule (targets, interleaving points, fault positions),
   not only the amounts.
3. Make S1 attribution precise: restrict reverts to undoing acknowledged writes.
4. Independent authors for scenarios and conventional tests. Blind mutant insertion.
5. Re-validate reproducibility across seeds, after a server restart, and on a second machine.
6. Correct `LIMITATIONS.md` on the "union of windows" claim (Finding 9) and the description of the
   PostgreSQL test count (Finding 8).

---

## Reproduction commands

Prerequisites: the setup in `README.md` (virtualenv in `.venv`, `pip install -e "backend[dev]"`, and a
PostgreSQL 16 server through `python -m gauntlet pg install|init|start`). The audit scripts are in `audit/`.
The benchmark data is the frozen evidence database `evidence/results-bench-f5316f9d4b.sqlite3`, which holds
the same benchmark the audit read from `.gauntlet/results.sqlite3`.

Open a Windows command prompt in the project directory (the folder containing `README.md`), then run:

```bat
set REPO=%CD%
set PY="%REPO%\.venv\Scripts\python.exe"

REM Integrity of the frozen evidence (expected: "results-bench-f5316f9d4b.sqlite3: OK" from sha256sum,
REM or compare certutil's hash with evidence\results-bench-f5316f9d4b.sqlite3.sha256)
certutil -hashfile "%REPO%\evidence\results-bench-f5316f9d4b.sqlite3" SHA256

REM Commands that write results must not touch the evidence file: use a working copy (ignored by git)
if not exist "%REPO%\.gauntlet" mkdir "%REPO%\.gauntlet"
copy /Y "%REPO%\evidence\results-bench-f5316f9d4b.sqlite3" "%REPO%\.gauntlet\audit-working-copy.sqlite3"
set GAUNTLET_RESULTS_DB=%REPO%\.gauntlet\audit-working-copy.sqlite3

%PY% -m gauntlet pg start

REM 1. tests (run from backend\)
cd /d "%REPO%\backend"
%PY% -m pytest -v -p no:cacheprovider
%PY% -m pytest -m pg -v -p no:cacheprovider

REM 3/4. exposing traces (read-only queries against the frozen evidence)
cd /d "%REPO%\audit"
%PY% audit_traces.py "%REPO%\evidence\results-bench-f5316f9d4b.sqlite3"

REM 4. sequential classification and 5. oracle audit (run from audit\; audit_sequential_retry imports audit_sequential)
%PY% audit_sequential.py
%PY% audit_sequential_retry.py
%PY% audit_oracle.py

REM benchmark report and a single-run replay with hash comparison (both use the working copy)
cd /d "%REPO%\backend"
%PY% -m gauntlet report --benchmark
%PY% -m gauntlet replay 165ca695d02c49e5
```

`gauntlet pg start` reports an error if the server is already running; that error can be ignored.

The breakdowns for items 2, 3 and 6 were produced with inline queries against `.gauntlet/results.sqlite3`
during the audit session. They are not saved as scripts, and the `replay` and `report` commands above
give the same information. `audit_sequential.py` creates and drops fresh databases and takes about 2
minutes. A full benchmark re-run (`python -m gauntlet bench --seeds 1,2,3`) takes about 25 minutes.
