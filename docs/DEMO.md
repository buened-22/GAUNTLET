# Demonstration runbook

Exact steps for a reviewer to run the prototype locally (Git Bash syntax; adapt `export` for other shells).
Nothing here evaluates an AI coding agent; candidate steps use labelled synthetic fixtures. All runs are
author-run on Windows 11 / Python 3.14 / PostgreSQL 16.14:

* every command was re-run on 2026-11-04 from a clean copy with a freshly installed PostgreSQL, before the
  FR-3 and A-02 commits;
* on 2026-11-05, at `2a73551`, an intermediate build on the private v0.4.1 development branch (before the
  v0.4.1 release; the identifier cannot be resolved in this repository, see
  [HISTORY.md](HISTORY.md#development-branch-identifiers-cited-in-the-documentation)), steps 2 (checksums
  and full suite), 4, 5, 6 and the API evidence check of step 8 were re-run, from `backend/` with the
  equivalent relative paths; the dashboard
  (step 7) and the frontend build were not ([archive/research/demo-readiness-report.md](archive/research/demo-readiness-report.md), archived).

The timed 5-minute presenter walkthrough and the presenter security notes written for the v0.4.1
demonstration are archived: [archive/research/demo-script.md](archive/research/demo-script.md) and
[archive/docs/DEMO_SECURITY_NOTES.md](archive/docs/DEMO_SECURITY_NOTES.md); the current security description
is [SECURITY_MODEL.md](SECURITY_MODEL.md). **`process` isolation is not a security
boundary and `strict` (VM/container) isolation is not implemented: do not run untrusted or AI-generated code
outside a disconnected, disposable VM.** The one such run so far is recorded in
[../research/gemini-attempt2-results.md](../research/gemini-attempt2-results.md).

## 0. What you are looking at

GAUNTLET checks whether migration code that passes ordinary tests stays correct when execution turns
adversarial (concurrent legacy writes, crashes, retries, repeated steps) during an online migration of
`orders.total NUMERIC(12,2)` to `orders.total_cents BIGINT`. An independent oracle judges each run from the
client-visible ledger and raw database snapshots. See README.md for the full background.

## 1. Setup (once)

Prerequisites: Python >= 3.11, Node >= 18 (for the dashboard and the portable PostgreSQL download).

Create and activate a virtual environment in the repository root:

```bash
# Windows, Git Bash (the shell used for every command in this runbook)
python -m venv .venv && source .venv/Scripts/activate
# Windows, PowerShell:   python -m venv .venv; .venv\Scripts\Activate.ps1
# Windows, cmd.exe:      python -m venv .venv && .venv\Scripts\activate.bat
# macOS / Linux:         python3 -m venv .venv && source .venv/bin/activate
```

Then, with the environment active:

```bash
pip install -c backend/constraints.txt -e "backend[dev]"      # exact verified versions (or omit -c ...)
python -m gauntlet pg install && python -m gauntlet pg init && python -m gauntlet pg start
python -m gauntlet pg check                                 # "is_pg16": true
```

## 2. Verify the frozen release and run the tests

```bash
# v0.1.0-v0.3.0 were developed in a separate repository (v0.3.0 = commit 694577b, see docs/HISTORY.md);
# here the frozen evidence is verified by its checksums:
(cd evidence && sha256sum -c *.sha256)                      # four OK lines
(cd backend && python -m pytest -q -m "not pg")             # unit tests only (no database)
(cd backend && python -m pytest -q)                         # full suite, about 10 min (much longer under disk load)
(cd frontend && npm install && npm run typecheck && npm run build)
```

The subshells keep you in the repository root whatever a command's exit status. The latest full run
(2026-11-14, Windows 11, Python 3.14.0, PostgreSQL 16.14) is **585 passed, 0 failed, 0 skipped** in about
12 minutes. Check the summary line for skips: tests that need PostgreSQL are
skipped, with a reason, when no server is reachable, and such a run is not a full run.

At commit `2a73551` the result was 510 passed, 1 failed (511 tests). The failing test,
`test_audit_hardening_pg.py::test_row_and_byte_budgets_stop_the_statement_without_buffering_it`, had a
`< 10 s` wall-clock bound around a query that PostgreSQL first materializes into a 668 MB temporary file
(audit finding FR-9); its query now streams, and the bound is unchanged. Several other tests keep
wall-clock bounds, and earlier runs under heavy disk load showed other timing-related failures that passed
when re-run alone.

## 3. Use a scratch results database

All commands below write to the results database named by `GAUNTLET_RESULTS_DB`. Use a scratch file
(never point it at `evidence/`):

```bash
export GAUNTLET_RESULTS_DB="$PWD/.gauntlet/demo.sqlite3"
export GAUNTLET_CANDIDATES_DIR="$PWD/.gauntlet/demo-candidates"     # optional: scratch candidate snapshots
cp evidence/results-bench-eed0c47427.sqlite3 "$GAUNTLET_RESULTS_DB"   # optional: start from the v0.3.0 benchmark
```

Steps 4–8 run from the repository root with the same shell, so these variables stay set.

Every GAUNTLET command, including read-only ones such as `report` and `serve`, adds missing tables (e.g.
`analyses`) to the database it opens. A copy of the evidence database therefore no longer matches
`evidence/*.sha256` once used; the frozen file itself is only safe if it is never opened directly.

## 4. Differential testing: a behavioural disagreement confirmed by the oracle

```bash
python -m gauntlet diff --impls ref_a_trigger,m1_stale_backfill --scenario S02_backfill_vs_legacy_writes --seed 1
```

Expect `CONFIRMED_VIOLATION`: the reference passes; the mutant (a backfill that writes cents computed from
a stale read) fails `S1_ACKED_DURABLE` and `S7_LINEARIZABLE`. Note the mutant's run id. With
`--impls ref_a_trigger,ref_b_dualwrite` the classification is `AGREEMENT` although the two strategies'
internal bookkeeping differs (normalization rule N3, docs/ANALYSES.md).

## 5. Schedule exploration and minimization

```bash
python -m gauntlet explore --scenario F01_backfill_race_probe --impl m1_stale_backfill
python -m gauntlet minimize <mutant run id from step 4> --invariant S1_ACKED_DURABLE
```

Exploration executes every interleaving of the fixture's race with at most one preemption (18 schedules),
finds the failing ones (5, one failure class) and replays the class representative; it claims
exhaustiveness only within that bound. Minimization reduces the 104-directive S02 schedule to a much
smaller one (19 directives in the recorded runs) that reproduces the same failure signature, is
1-minimal with respect to unit deletion (not globally minimal), and is replayed independently; the
original run is unchanged.

## 6. Evaluate an external candidate (synthetic fixture, trusted)

```bash
python -m gauntlet candidate isolation                                      # strict: unavailable here
python -m gauntlet candidate validate backend/tests/candidates/synthetic_stale_backfill
python -m gauntlet candidate register backend/tests/candidates/synthetic_stale_backfill   # cand-6478453bb5dd4e78
python -m gauntlet candidate evaluate --id cand-6478453bb5dd4e78 --isolation process --trusted-dev-env \
    --scenarios S01_baseline_migration,S02_backfill_vs_legacy_writes --seeds 1 --refs ref_a_trigger \
    --explore-max-schedules 18 --minimize-budget 20 --out .gauntlet/candidate-summary.md
```

`--trusted-dev-env` is required because `process` isolation is **not a security boundary**; the fixture is
trusted code written for testing. Without it the evaluation is blocked (exit code 3), and `--isolation strict`
is always blocked because VM/container isolation is not implemented. The summary lists
the candidate's source hash, isolation record, per-scenario outcomes and differential classifications,
exploration coverage, minimization results, the experiment report id and its evidence-manifest check.

## 7. Dashboard

```bash
python -m gauntlet serve &                                 # API on :8000, same GAUNTLET_RESULTS_DB
cd frontend && npm run dev                                 # http://localhost:5173
```

Pages: Overview (latest benchmark in the database), Run benchmark, Compare runs, Differential, Exploration,
Minimization, Experiments (with *Verify evidence*), Candidates (evaluation summaries).

## 8. Verify artifacts

* Frozen evidence: `sha256sum -c` in `evidence/` (step 2).
* A candidate snapshot: `python -m gauntlet candidate verify <candidate id>`.
* An experiment report's evidence (needs the API from step 7): take the id from the summary line
  ``Experiment report `expe-…` `` of step 6, then

  ```bash
  EXPERIMENT_ID=expe-xxxxxxxxxxxx      # replace with the id from your candidate summary
  curl "http://127.0.0.1:8000/api/experiments/$EXPERIMENT_ID/verify"
  ```

  which re-hashes every run, benchmark summary and analysis the report used. Expected for the step-6
  evaluation: `{"ok":true,"checked":5,"missing":[],"modified":[]}`; an unknown id returns HTTP 404.
* Any run: `python -m gauntlet replay <run id>` compares schedule, trace and outcome hashes and the verdict.

## 9. What this demonstrates, and what it does not

Demonstrates: deterministic, replayable adversarial execution; an independent oracle that detects all eight
hand-written mutants by their intended invariants with no failures on the two references; differential
classification that separates harmless from meaningful differences; bounded exhaustive interleaving search;
failure minimization with preserved signatures; reports whose every number traces to hashed run records; an
external-candidate pipeline with content-addressed snapshots.

Does not demonstrate: any result about AI coding agents (none has been evaluated); safe execution of
untrusted code (no verified security boundary exists on the development machine); generalization beyond
one task family, PostgreSQL 16 and the hand-written implementations; independent reproduction (all
validation is author-run).
