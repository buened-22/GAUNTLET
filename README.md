# GAUNTLET

**Adversarial evaluation of database-migration code under concurrency, partial failure and retries.
Research prototype. Software version 0.4.0 (proposed, untagged); benchmark version 0.3.0.**

> **Status, in one paragraph.** GAUNTLET is an *instrument* for testing whether migration code that passes
> ordinary tests stays correct when execution turns adversarial. It has been validated only on
> hand-written reference implementations, hand-written mutants and labelled synthetic fixtures. **No AI
> coding agent has been evaluated**, and **no execution mode is a security boundary** for untrusted code.
> All validation was run by the author and has not been independently reproduced.

## The problem

Online schema migrations run while old and new application versions share one database. A migration
that changes how money is stored, for example `orders.total NUMERIC(12,2)` → `orders.total_cents BIGINT`,
must stay exact to the cent while a legacy writer keeps writing, a backfill runs in batches, processes
crash between transactions, responses are lost and clients retry, and steps are repeated or resumed.

Conventional functional tests run one client at a time, against a database nobody else is writing to,
with no crashes. A backfill that copies a value it read a moment ago, a dual write split over two
transactions, or a retry that is applied twice all pass such tests. They fail only under particular
interleavings or faults, which ordinary tests do not produce and cannot reproduce on demand.

GAUNTLET makes those conditions explicit, controllable and replayable, and judges every run with an
oracle that does not trust the implementation's own reports.

## How it works

```mermaid
flowchart LR
    S["Scenario + seed<br/>(schedule document)"] --> E["Adversarial execution engine<br/>statement-level gates, crashes,<br/>lost responses, retries"]
    I["Implementation under test<br/>(reference, mutant or candidate)"] --> E
    E --> L["Client ledger<br/>+ raw DB snapshots"]
    L --> O["Independent oracle<br/>12 invariants, per-order<br/>linearizability, exact cents"]
    O --> R["Run record<br/>schedule / trace / outcome hashes"]
    R --> D["Differential testing"]
    R --> X["Bounded schedule exploration"]
    R --> M["Failure minimization"]
    R --> P["Experiment reports<br/>+ hashed evidence manifest"]
```

1. A **scenario** and **seed** produce a schedule document: which client calls what, when the migrator
   runs, where to crash, which response to drop, how statements interleave.
2. The **engine** runs each actor (legacy app V1, new app V2, migrator) in its own OS process. Every SQL
   statement waits at a gate until a single-threaded controller grants it, so interleavings, crashes and
   lost responses happen at exact statement boundaries, reproducibly, without knowing the implementation's
   code.
3. The **oracle** judges the run from what clients observed (an append-only ledger) and raw database
   snapshots: 12 safety, recovery and compatibility invariants, including a per-order linearizability
   check in exact integer cents. It imports no implementation code.
4. The run is stored with hashes of its schedule, statement trace and outcome; `replay` re-executes it
   and compares them.

## What the prototype shows (synthetic results)

Revalidated during the v0.4.0 release review; details and caveats in
[research/results-summary.md](research/results-summary.md).

| Result | Evidence |
|---|---|
| The oracle passes both reference strategies on every benchmark run (36/36) and kills all 8 hand-written mutants with their *intended* invariant | Frozen v0.3.0 benchmark `bench-eed0c47427`: 180 runs (6 scenarios × 10 implementations × seeds 1–3), 121 pass, 59 fail, 0 inconclusive, 0 infrastructure errors; totals recomputed from the raw run rows |
| Runs are logically reproducible | 60/60 benchmark re-runs identical in schedule, trace and outcome hash and verdict; demo replays identical |
| 4 mutants pass all 10 conventional tests but fail adversarial runs (m1, m3, m7, m8; m2's conventional result is inconclusive in v0.3.0, see below) | Same benchmark |
| Differential testing separates harmful from harmless differences | `ref_a_trigger` vs `m1_stale_backfill` on S02: **confirmed violation**; `ref_a_trigger` vs `ref_b_dualwrite`: **agreement** despite different internal bookkeeping |
| Bounded exploration finds and replays the backfill race | `F01_backfill_race_probe` × m1: all 18 schedules with ≤ 1 preemption executed, 5 fail in 1 failure class, replay reproduced |
| Minimization shrinks a failing schedule and keeps its failure signature | S02 × m1: 104 → 19 directives (390 → 51 trace events), 1-minimal with respect to unit deletion, independent replay matches |
| The external-candidate pipeline works end to end on a **synthetic** fixture | Content-hashed snapshot, evaluation, experiment report, evidence manifest 5/5 unchanged |

## Capabilities and limitations

| Capability | Status | Bound / caveat |
|---|---|---|
| Statement-level adversarial execution, crash / lost-response / retry injection | Implemented, tested | Statement granularity only; READ COMMITTED; PostgreSQL 16 |
| Independent oracle (12 invariants) | Implemented, tested | Same author as references and mutants; diagnoses mean "consistent with", not root cause |
| Benchmark matrix and conventional baseline | Implemented; v0.1.0–v0.3.0 results frozen | One task family; one small conventional suite |
| Differential testing | Implemented, tested | Agreement is not correctness |
| Schedule exploration | Implemented, tested | One interleave directive, ≤ N preemptions (default 1); no crash-point exploration, no partial-order reduction |
| Failure minimization | Implemented, tested | Unit deletion only; 1-minimal, not globally minimal |
| Experiment reports with evidence manifests | Implemented, tested | Manifest stored in the same database (detects accidental change, not tampering) |
| External-candidate pipeline | Implemented; synthetic fixtures only | `process` isolation is **not** a security boundary; `strict` isolation not implemented |
| AI-agent evaluation | **Not done** | Requires a verified isolation boundary first |
| Independent reproduction | **Not done** | All validation author-run on one Windows machine |

Supported platforms: Windows 11 (all recorded results). Linux and macOS are expected to work for trusted
code but have **not** been verified. Python ≥ 3.11 (recorded on 3.14.0), PostgreSQL 16 (recorded on
16.14), Node ≥ 18 for the dashboard and the portable PostgreSQL download (recorded on 24.11).

## Quick start

```bash
python -m venv .venv && source .venv/Scripts/activate      # macOS/Linux: source .venv/bin/activate
pip install -c backend/constraints.txt -e "backend[dev]"
python -m gauntlet pg install && python -m gauntlet pg init && python -m gauntlet pg start
export GAUNTLET_RESULTS_DB="$PWD/.gauntlet/demo.sqlite3"    # scratch results database
cd backend
python -m gauntlet diff --impls ref_a_trigger,m1_stale_backfill --scenario S02_backfill_vs_legacy_writes --seed 1
# -> CONFIRMED_VIOLATION  behaviour differs and the oracle FAILs: m1_stale_backfill: S7_LINEARIZABLE, S1_ACKED_DURABLE
```

The full walkthrough is in [docs/DEMO.md](docs/DEMO.md) and [research/demo-script.md](research/demo-script.md).

## Documentation

| Document | Contents |
|---|---|
| [docs/RESEARCH_METHODOLOGY.md](docs/RESEARCH_METHODOLOGY.md) | Research question, fault model, invariants and outcome terms, references and mutants, differential testing, exploration bounds, minimization, replay and evidence integrity, threats to validity |
| [docs/RESEARCH_DESIGN.md](docs/RESEARCH_DESIGN.md) | Hypotheses, variables, baselines, threats to validity |
| [docs/INVARIANTS.md](docs/INVARIANTS.md), [docs/FAULT_CATALOG.md](docs/FAULT_CATALOG.md), [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) | Oracle definitions, mutants, engine design |
| [docs/ANALYSES.md](docs/ANALYSES.md), [docs/CANDIDATES.md](docs/CANDIDATES.md), [docs/AGENT_EVALUATION_PROTOCOL.md](docs/AGENT_EVALUATION_PROTOCOL.md) | Analysis algorithms, external candidates, the protocol for a future agent study |
| [docs/SECURITY_MODEL.md](docs/SECURITY_MODEL.md), [SECURITY.md](SECURITY.md) | What each execution mode does and does not guarantee; reporting vulnerabilities |
| [docs/REPRODUCIBILITY.md](docs/REPRODUCIBILITY.md), [docs/DEMO.md](docs/DEMO.md) | Clean-checkout reproduction and the demonstration runbook |
| [docs/LIMITATIONS.md](docs/LIMITATIONS.md) | What the prototype does not show |
| [research/](research/) | Technical report, results summary, demo script, open questions |
| [docs/HISTORY.md](docs/HISTORY.md) | Why this repository starts at v0.4.0; original commit identifiers of v0.1.0–v0.3.0 |
| [docs/RELEASE_v0.4.0.md](docs/RELEASE_v0.4.0.md) | Proposed release notes and publication checklist |
| `docs/RESULTS-v0.3.0.md`, `docs/RESULTS-v0.2.0.md`, `docs/RESULTS.md`, `evidence/` | Frozen benchmark records (never edited) |

## Release history

* **0.4.0 (proposed, this tree; not tagged; first release in this repository).** Adds differential testing, bounded schedule exploration,
  failure minimization, experiment reports and the external-candidate pipeline (process sandbox,
  fail-closed strict mode), plus the release documentation. No benchmark scenario, oracle rule,
  reference, mutant or recorded result changed; the benchmark version stays 0.3.0.
* **0.3.0** (benchmark version 0.3.0; released as tag `v0.3.0`, commit `694577b`, in the separate development
  repository, see [docs/HISTORY.md](docs/HISTORY.md)). Two changes:

  * **S04 gains one directive.** After the backfill has populated `total_cents`, the migrator repeats the
    completed `expand` step a second time (`repeat(expand)`). The earlier repeat runs before the backfill,
    when `total_cents` is still empty, so R2 (repeated steps are idempotent) could not see an `expand` that
    destroys stored values. The new repeat gives R2 something to lose. No benchmark implementation has
    that defect, so no verdict changed (see below).
  * **Conventional-suite reporting keeps pass, fail and infrastructure error distinct.** Previously an
    infrastructure error in a conventional test was reported as a test failure.

* **0.2.0.** Fixed the two highest-priority findings of the self-audit
  ([docs/GAUNTLET_VALIDATION_AUDIT.md](docs/GAUNTLET_VALIDATION_AUDIT.md), which was written against v0.1.0):

  * the oracle identifies a logical write by its idempotency key (Finding 1);
  * seeds vary schedule structure, not only amounts, in S01, S03, S04 and S05 (Finding 2).

* **0.1.0.** First benchmark. The v0.1.0 and v0.2.0 benchmarks are kept unchanged as historical evidence.

## Benchmark results (frozen, v0.1.0 to v0.3.0)

All three result files are generated by `python -m gauntlet bench` from recorded runs only, and nothing in
them is edited by hand. Each matrix is 10 implementations × 6 scenarios × seeds 1–3 on PostgreSQL 16.14.
The raw databases are frozen, with SHA-256 checksums, in [evidence/](evidence/).

| Metric | **v0.3.0 (current)**: `bench-eed0c47427`, [docs/RESULTS-v0.3.0.md](docs/RESULTS-v0.3.0.md) | v0.2.0 (historical): `bench-7c4395ec2b`, [docs/RESULTS-v0.2.0.md](docs/RESULTS-v0.2.0.md) | v0.1.0 (historical): `bench-f5316f9d4b`, [docs/RESULTS.md](docs/RESULTS.md) |
|---|---|---|---|
| Runs completed | 180: 121 pass, 59 fail, **0 inconclusive, 0 infrastructure errors** | 180: 121 pass, 59 fail, 0 inconclusive, 0 infrastructure errors | 180: 125 pass, 55 fail, 0 inconclusive, 0 infrastructure errors |
| Reference implementations | `ref_a_trigger` 18/18 and `ref_b_dualwrite` 18/18 pass; 10/10 conventional tests each | same | same |
| Mutants detected / killed by their *intended* invariant | 8/8 / 8/8 | 8/8 / 8/8 | 8/8 / 8/8 |
| Invariant violations (FAIL results) | 108 | 108 | 98 |
| Reproducibility re-runs identical (schedule, trace, outcome, verdict) | 60/60 | 60/60 | 60/60 |
| Mutants that pass all 10 conventional tests but fail adversarial runs | 4: m1, m3, m7, m8 | 5: m1, m2, m3, m7, m8 | 5: m1, m2, m3, m7, m8 |
| Mutants whose conventional result is inconclusive (infrastructure error, no failure) but fail adversarial runs | 1: m2 (CT03, see below) | 0 | 0 |

**v0.3.0 compared with v0.2.0.** Every one of the 180 cells has the same verdict and the same invariant
statuses. The 150 non-S04 cells are identical in schedule, trace, outcome, snapshots and evidence. The 30
S04 cells differ only by the inserted directive and its direct consequences: one extra `expand` attempt,
two extra snapshots, one extra R2 entry, and shifted sequence numbers. The new repeat changed no result
because no implementation here destroys data on a repeated `expand`. A test-only implementation that does
is detected by R2 in S04 (`backend/tests/test_r2_repeat_coverage.py`), but that implementation is not part
of the benchmark. The benchmark version is part of every schedule hash, so v0.2.0 and v0.3.0 runs are not
run-for-run interchangeable.

**v0.2.0 compared with v0.1.0.** The two benchmarks are **not run-for-run comparable**. The same seed
number produces a different schedule in S01, S03, S04 and S05 under v0.2.0. Re-evaluating every v0.1.0 run
with the v0.2.0 oracle gives identical results, so the differences between those columns come from the new
schedules, not from the oracle change.

### m2's conventional result in v0.3.0

The change in m2's conventional classification is **not** a measured change in m2's behaviour. In the
v0.3.0 benchmark, conventional test CT03 for `m2_nonatomic_dual_write` ended in an **infrastructure error**: the
harness's 20-second step timeout fired on a statement that neither completed nor waited on a lock. Its
cause was not identified. It is recorded as an infrastructure error, which is neither a pass nor a
failure, so m2's conventional result for this benchmark is **inconclusive**.

Two further records exist, and they should not be confused with the benchmark's result:

* **Original diagnostic database vs corrected frozen evidence.** The benchmark was first written to a
  git-ignored diagnostic database. Its summary was produced by the old reporting code, which labelled the
  CT03 infrastructure error as a test failure ("fails CT03"). The frozen evidence database
  `evidence/results-bench-eed0c47427.sqlite3` is a copy in which only the benchmark summary was recomputed
  by the corrected reporting code from the same recorded data. All 240 run rows are identical. Nothing was
  re-executed.
* **Supplementary conventional re-run.** On 2026-10-19 the ten conventional suites were run once more, each
  exactly once without retries (`evidence/conventional-rerun-bench-eed0c47427.json`). There were no
  infrastructure errors, and m2 passed 10/10, matching v0.2.0 test for test. This re-run is a separate
  record. It does not replace the benchmark's recorded result, it does not show that the original error
  did not occur, and it does not explain its cause.

Details: [evidence/README-bench-eed0c47427.md](evidence/README-bench-eed0c47427.md).

### Interpretation

How to read the conventional rows (interpretation, not a measured quantity):

* Only **m1** (a backfill race with a concurrent legacy write) and **m2** (a crash between two transactions
  of a dual write, followed by an idempotent retry) *require* interleaving control or fault injection to
  expose. In v0.2.0 and in the supplementary v0.3.0 re-run, m2 passed all 10 conventional tests.
* **m3**, **m7** and **m8** survive because the conventional suite happens not to contain a test that would
  catch them:
  * m3: a v2 duplicate-key replay;
  * m7: a v2 charge on a legacy row;
  * m8: a legacy write after the backfill.

  Sequential tests could catch all three. The suite was frozen before results were looked at and has not
  been tuned. See the validity threats in [docs/RESEARCH_DESIGN.md](docs/RESEARCH_DESIGN.md).
* Detection is schedule-dependent in places.
  * **v0.3.0 and v0.2.0** (identical detection matrices):
    * m5 is detected in 2 of 3 seeds for S05;
    * m6 is detected in 2 of 3 seeds for S02 and S05;
    * m8 is detected in 2 of 3 seeds for S04;
    * m5 is never detected in S06.

    In each case the escaping seed's schedule gives the mutant no observable effect. For example, every
    row m5 skipped was later re-synced by the trigger through another write.
  * **v0.1.0:** m6 was detected in 2 of 3 seeds for S01 and S02, and m5 was never detected in S06.
  * New detections from the v0.2.0 schedules, unchanged in v0.3.0:
    * m7 in S01 (3/3), from seeded v2 charges on rows not yet backfilled;
    * m8 in S04 (2/3). The v0.2.0 documentation attributed this to legacy writes interleaved with the
      retried backfill batch. That attribution is too specific. An exploratory, author-run
      sequential ablation (not part of the frozen evidence) detected m8 in S04 with the same counts
      without that interleaving. This suggests that legacy writes during the backfill, in any order, are
      enough.

## Analyses and external candidates (added in 0.4.0)

Four capabilities build on the same runner, oracle and results store; they change no benchmark
scenario, oracle rule or recorded result, and no AI agent has been evaluated with them:

* **differential testing** of two or more implementations on the same schedule, with documented
  normalization and four classifications (agreement, confirmed violation, unconfirmed disagreement,
  inconclusive);
* **bounded schedule exploration** of one interleave directive (preemption-bounded systematic search;
  exhaustive only within its stated bound);
* **failure minimization** of a saved failing run by delta debugging over schedule units;
* **experiment reports** with explicit metric definitions, traceable run ids and a hashed manifest.

See [docs/ANALYSES.md](docs/ANALYSES.md) for algorithms, rules and limitations.

**External candidates**: an implementation produced outside GAUNTLET can be validated,
snapshotted (content-hashed) and evaluated with all of the above through `python -m gauntlet candidate ...`
under a process sandbox that is explicitly *not* a security boundary; strict (container) isolation is not
implemented and fails closed. See [docs/CANDIDATES.md](docs/CANDIDATES.md) and the agent-evaluation
protocol in [docs/AGENT_EVALUATION_PROTOCOL.md](docs/AGENT_EVALUATION_PROTOCOL.md). No AI coding agent has
been evaluated; only labelled synthetic fixtures have been run.

## Repository layout

```
backend/
  gauntlet/
    actor/        child.py (actor subprocess + statement gates), proxy.py (parent handle, lock-wait detection)
    engine/       controller.py (directives, fault injection), runner.py (one run), observe.py (raw snapshots)
    impls/        legacy.py (V1), reference_a.py, reference_b.py, mutants.py, registry.py, base.py (contract)
    oracle/       ledger.py, linearizability.py, checker.py, invariants.py
    scenarios/    script.py (schedule DSL, seeded workloads), catalog.py (S01–S06)
    pg/           provision.py (per-run DB, P0 schema), localpg.py (Docker-free PostgreSQL 16)
    candidates/   external candidates: contract, snapshots, loader, sandbox, guard, evaluate
    api/          app.py (FastAPI), analyses.py (differential, exploration, minimization, experiments, candidates)
    differential.py  explore.py  minimize.py  experiment.py  execution.py  schedules.py
    bench.py  conventional.py  compare.py  report.py  store.py  faults.py  money.py  cli.py
  tests/          unit tests + PostgreSQL integration tests + API tests; tests/candidates/ synthetic fixtures
  constraints.txt exact verified dependency versions
frontend/         React + TypeScript + Vite dashboard
docs/             RESEARCH_METHODOLOGY, RESEARCH_DESIGN, INVARIANTS, FAULT_CATALOG, ARCHITECTURE, ANALYSES,
                  CANDIDATES, AGENT_EVALUATION_PROTOCOL, SECURITY_MODEL, REPRODUCIBILITY, LIMITATIONS, DEMO,
                  RELEASE_v0.4.0, agent_task/ (task bundle for a future agent study),
                  RESULTS-v0.3.0, RESULTS-v0.2.0, RESULTS (v0.1.0), GAUNTLET_VALIDATION_AUDIT (v0.1.0 audit)
research/         technical report, results summary, demo script and transcript, open questions
evidence/         frozen benchmark databases with SHA-256 checksums (v0.3.0, plus historical v0.2.0 and
                  v0.1.0) and the supplementary v0.3.0 conventional re-run record
audit/            scripts used by the validation audit (see audit/PROVENANCE.md)
docker-compose.yml   PostgreSQL 16 service (alternative to the local cluster; untested here)
PROJECT_SPEC.md      full specification + implementation status
```

## Setup

Requirements: Python ≥ 3.11 (developed on 3.14), Node ≥ 18 (developed on 24), and a PostgreSQL 16
server. All commands below are run from the repository root.

```bash
python -m venv .venv
# Windows: .venv\Scripts\activate      macOS/Linux: source .venv/bin/activate
pip install -e "backend[dev]"
# reproducible: pin the exact verified versions
# pip install -c backend/constraints.txt -e "backend[dev]"
```

### PostgreSQL 16: option 1, no Docker (what the results were produced with)

```bash
python -m gauntlet pg install   # downloads portable PostgreSQL 16.14 binaries via npm into .gauntlet/pgdist
python -m gauntlet pg init      # creates .gauntlet/pgdata (user gauntlet / password gauntlet, port 55432)
python -m gauntlet pg start
python -m gauntlet pg check     # prints server version
```

You can also point `GAUNTLET_PG_BIN` at an existing PostgreSQL 16 `bin/` directory.

### PostgreSQL 16: option 2, Docker

```bash
docker compose up -d            # same port and credentials as option 1
```

This path is provided but **was not tested**: Docker was not available on the development machine.

### Any other PostgreSQL 16 server

Set `GAUNTLET_PG_DSN=postgresql://user:pass@host:port/postgres`. The user needs `CREATEDB`, because each
run creates and drops its own database, and `CREATEROLE` for external candidates, which run under a
per-run restricted role.

## Commands

```bash
python -m gauntlet scenarios                         # list scenarios
python -m gauntlet impls                             # list references and mutants
python -m gauntlet run --scenario S02_backfill_vs_legacy_writes --impl m1_stale_backfill --seed 1
python -m gauntlet replay <run_id>                   # re-execute a persisted run, compare hashes
python -m gauntlet report <run_id>                   # Markdown report for one run
python -m gauntlet conventional --impl ref_a_trigger # conventional functional suite
# full benchmark (tens of minutes), into its own database and a new report file
GAUNTLET_RESULTS_DB="$PWD/.gauntlet/results-new.sqlite3" python -m gauntlet bench --seeds 1,2,3 --write-results docs/RESULTS-new.md
python -m gauntlet bench --impls ref_a_trigger,m1_stale_backfill --scenarios S02_backfill_vs_legacy_writes --seeds 1 --no-conventional
```

`docs/RESULTS.md` (v0.1.0), `docs/RESULTS-v0.2.0.md` and `docs/RESULTS-v0.3.0.md` are records of specific
benchmarks. Write new benchmarks to a new file and, ideally, a new results database. Never overwrite the
recorded ones.

Every `run` is persisted to `.gauntlet/results.sqlite3` and is visible in the dashboard.

## Tests

Proposed v0.4.0 (release review, 2026-10-22, Windows 11, Python 3.14.0, PostgreSQL 16.14, run by the
author): **409 passed, 0 failed** (`python -m pytest`, of which 67 need PostgreSQL), plus `npm run
typecheck` and `npm run build`, both in the working tree and in a clean copy installed with
`backend/constraints.txt`. The numbers below describe the frozen v0.3.0 release. A step-by-step demonstration is in [docs/DEMO.md](docs/DEMO.md).

```bash
cd backend
python -m pytest            # everything; PostgreSQL tests skip (with a reason) if no server is reachable
python -m pytest -m "not pg"  # unit tests only (no database)
```

Last recorded result for v0.3.0 (Windows 11, Python 3.14.0, PostgreSQL 16.14), run by the author:
**231 passed, 0 skipped, 0 failed**. (v0.2.0 had 215 tests and v0.1.0 had 75.) That breaks down as 207
tests that need no database:

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
  repeated `expand` erases `total_cents` fails R2 (it would have passed under the v0.2.0 S04 schedule and
  is not in the benchmark registry).

Tests specifically required by the specification:

* `test_oracle_detects_wrong_final_state_even_when_app_reported_success`
* `test_inconclusive_run_is_never_reported_as_pass`
* `test_infra_error_is_never_pass`
* `test_unreachable_database_is_infra_error_not_pass`

All four are in `backend/tests/`.

## Dashboard

```bash
python -m gauntlet serve                 # API on http://127.0.0.1:8000
cd frontend && npm install && npm run dev  # UI on http://localhost:5173 (proxies /api to :8000)
```

| View | What it shows |
|---|---|
| **Overview** | Purpose, benchmark version, counts, PostgreSQL status, latest benchmark metrics (shown as *Unavailable* if none has run), detection matrix, mutant validation table |
| **Run benchmark** | Pick scenario, implementation and seed; start a run; status exactly as reported by the backend; persisted run list with selection for comparison |
| **Run → Execution timeline** | Swimlane of every event (statements by class, invocations, responses, lost responses, lock waits, crashes, snapshots, stage markers, violation triggers), filterable event table, per-event JSON plus ledger entry |
| **Run → Invariant report** | All 12 invariants: pass / fail / inconclusive / n/a / infra error, readable evidence, jump to the triggering event |
| **Run → Client ledger / Oracle snapshots** | The raw inputs the oracle used |
| **Run → Research report** | Markdown report generated from the run record (downloadable) |
| **Compare runs** | Configuration, schedule / trace / outcome equality, first divergence, invariant differences, outcome and final-state differences |
| **Scenario library** | Scenarios (with a per-seed schedule preview), implementations and mutations, invariants, fault catalog |
| **Research report** | Report generated from the latest completed benchmark |
| **Differential / Exploration / Minimization** | Start and inspect the analyses described above; replay and minimize actions on run detail |
| **Experiments** | Experiment reports with *Verify evidence* (re-hashes the manifest) |
| **Candidates** | Registered external candidates, snapshot integrity and evaluation summaries |

## How a run works (short version)

1. `scenario.build(seed)` produces a directive list. Its hash, the `schedule_hash`, depends only on
   (scenario, seed, version).
2. A fresh database receives the harness-owned legacy schema and seeded orders.
3. The controller executes directives:
   * `call` / `run` / `run_until(before_write | before_commit | after_commit)`;
   * `crash` / `restart` / `lose_response` / `retry`;
   * `interleave` (seeded random, statement-level), `drain`, `repeat`, `snapshot`.

   Each step is an event with a logical sequence number. Lock waits are detected with
   `pg_blocking_pids()`, so they are logical facts rather than timeouts.
4. The oracle evaluates S1–S7, R1–R4 and C1 from the ledger and the snapshots, and the run is persisted
   with `trace_hash` and `outcome_hash` for reproducibility checks.

See [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) and [docs/INVARIANTS.md](docs/INVARIANTS.md).

## Limitations (summary)

* No AI agent has been evaluated yet. The candidate pipeline exists, but no execution mode is a security
  boundary: `process` isolation does not stop host-file reads (including the oracle), native-code network
  access or guard bypass, and `strict` (container/VM) isolation is not implemented. See
  [docs/SECURITY_MODEL.md](docs/SECURITY_MODEL.md).
* Interleavings are explored at SQL-statement granularity: scripted or seeded-random in the benchmark,
  and exhaustively only within one interleave directive and a preemption bound (default 1) in
  exploration.
* The diagnoses S1/S2/S4/S6 mean "consistent with", not "root cause".
* The conventional baseline is one small suite written by the same author.
* All validation (benchmarks, reproducibility checks, tests, the self-audit) was run by the author. None
  of it has been independently reproduced.
* One conventional test in the v0.3.0 benchmark (m2, CT03) ended in an infrastructure error whose cause
  is unknown.
* Docker Compose is untested.
* Each run takes about 4–10 s on Windows, dominated by process start-up.

Full list: [docs/LIMITATIONS.md](docs/LIMITATIONS.md).

## Contributing, licence and citation

* Contributions: see [CONTRIBUTING.md](CONTRIBUTING.md). Independent reproduction reports are the most
  valuable contribution.
* Security: see [SECURITY.md](SECURITY.md).
* Licence: [Apache License 2.0](LICENSE). It covers the whole repository, including the frozen evidence
  and results records.
* Citation: see [CITATION.cff](CITATION.cff).
