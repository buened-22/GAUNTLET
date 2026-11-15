# GAUNTLET

**Adversarial evaluation of database-migration code under concurrency, partial failure and retries.
Research prototype. Software version 0.4.1; benchmark version 0.3.0.**

> **Status, in one paragraph.** GAUNTLET is an *instrument* for testing whether migration code that passes
> ordinary tests stays correct when execution turns adversarial. It has been validated only on
> hand-written reference implementations, hand-written mutants and labelled synthetic fixtures. **No study
> of an AI coding agent has been run**: one candidate generated with Gemini has been evaluated once, inside a
> disconnected VM, as a demonstration ([research/gemini-attempt2-results.md](research/gemini-attempt2-results.md)). **No execution mode is a security boundary** for untrusted code.
> All validation was run by the author and has not been independently reproduced.

> [!WARNING]
> **GAUNTLET does not sandbox untrusted code.** `process` mode is a development sandbox for code you trust:
> a candidate runs as your operating-system user, next to a harness that holds the database administrator
> credentials, and candidate code can read your files and open network connections (plain Python is enough
> to switch off GAUNTLET's in-process guard). `strict` (container / VM)
> isolation is not implemented and always refuses to run. **Do not run AI-generated or otherwise untrusted
> candidates in `process` mode on a machine you care about.** If you must evaluate such code, run GAUNTLET
> inside a disposable VM with no network and no shared folders. See [SECURITY.md](SECURITY.md).

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
| External-candidate pipeline | Implemented; exercised with synthetic fixtures and one AI-generated candidate | `process` isolation is **not** a security boundary; `strict` isolation not implemented |
| AI-agent evaluation | **One demonstration run; no study** | One Gemini-generated candidate, one seed, inside a disconnected VM. A study needs a fixed protocol, many attempts and a verified isolation boundary |
| Independent reproduction | **Not done** | All validation author-run |

Supported platforms: **Windows 11** is where the benchmark and every recorded result were produced. The
full test suite and the demonstration script also pass in continuous integration on **Windows Server 2025**
(Python 3.14, the portable PostgreSQL 16.14) and on **Ubuntu 26.04.1** (Python 3.11 and 3.14, PostgreSQL 16
from `docker-compose.yml`). The benchmark itself has not been re-executed on Linux, so identical hashes
there are not established. **macOS** has not been run at all. Node ≥ 18 for the dashboard and the portable
PostgreSQL download (recorded on 24.11). On Linux the tests that check the enforcement of `process` mode's
resource limits are skipped: those limits are applied there but **not verified**, and the process-count
limit is not enforced.

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

A one-command demonstration runs the same comparison, replays the failure, and shows both safety gates
refusing to execute a candidate (about 30 s; it never executes candidate code):

```bash
bash scripts/run_demo.sh          # from the repository root; expect "checks passed: 9, failed: 0"
```

It needs `bash` (Git Bash on Windows), the virtual environment's `python` on `PATH`, a running PostgreSQL
16 server and `sha256sum` (not part of macOS; install GNU coreutils there). It writes only to a fresh
directory under `.gauntlet/` and exits with status 0 only if every check passed.

The full walkthrough is in [docs/DEMO.md](docs/DEMO.md) and [research/demo-script.md](research/demo-script.md).

## First run on AI-generated code (one attempt, not a study)

On 2026-11-07 one migration generated with Gemini was evaluated once, inside a disconnected VM. Full record:
[research/gemini-attempt2-results.md](research/gemini-attempt2-results.md).

| Scenario (seed 1) | Candidate `cand-232794272c0fa534` | `ref_a_trigger` | Classification |
|---|---|---|---|
| S01 baseline migration | pass | pass | agreement |
| S02 backfill vs legacy writes | pass | pass | agreement |
| S03 lost acknowledgement and retry | pass | pass | agreement |
| S04 crash and resume | pass | pass | agreement |
| S05 premature finalization | **fail: `C1_COMPATIBILITY`** | pass | **confirmed violation** |
| S06 unbackfilled records | pass | pass | agreement |

In S05 the migration is asked to finalize and contract while old (v1) application instances are still
registered. The reference refuses until they have drained. The candidate's `migrator_finalize` and
`migrator_contract` do not check for registered v1 instances, and a later v1 request fails. The failing
schedule was minimized from 48 to 16 directives and replayed with the same trace and outcome.

How to read this, and how not to:

* It is **one attempt on one seed**. It demonstrates that the pipeline works on real AI output. It is not
  a measurement of Gemini, says nothing about how often any model makes this mistake, and supports no
  failure rate.
* No lost acknowledged write was found in this attempt; the other five scenarios and a bounded exploration
  of the backfill race (15 schedules) passed. Passing those schedules does not show the code is correct.
* Whether this candidate passes the conventional test suite was not measured.
* The raw run records were produced inside the VM and are **not in this repository**, and neither are the
  candidate's source files. The published values were transcribed from the VM; the SHA-256 hashes of the
  two candidate files, of the canonical source and of the two result files are recorded so that the
  originals can be matched to this record. Until those files are published, a reader cannot re-verify this
  run independently, unlike the frozen benchmark in [evidence/](evidence/).

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
| [docs/HISTORY.md](docs/HISTORY.md) | Why this repository has one commit per release; identifiers of the unpublished development commits cited in the documentation |
| [docs/RELEASE_v0.4.1.md](docs/RELEASE_v0.4.1.md), [docs/RELEASE_v0.4.0.md](docs/RELEASE_v0.4.0.md), [CHANGELOG.md](CHANGELOG.md) | Release notes |
| [docs/THIRD_PARTY.md](docs/THIRD_PARTY.md), [NOTICE](NOTICE) | Dependencies and their licences; copyright notice |
| `docs/RESULTS-v0.3.0.md`, `docs/RESULTS-v0.2.0.md`, `docs/RESULTS.md`, `evidence/` | Frozen benchmark records (never edited) |

## Release history

* **0.4.1 (audit hardening; first public release).** Harness-owned actor database connections (audit finding A-01: candidate
  code no longer shares access to a database connection), a non-superuser observer role for candidate
  databases (A-02), evidence-based classification of statement timeouts (FR-3), request-origin checks on
  the local API, actor protocol validation, robust cleanup, a bounded monitor lock wait and more robust SQL
  classification, with regression tests. No benchmark scenario, oracle rule, reference, mutant or recorded
  result changed: a re-execution of the benchmark on 2026-11-12 matched the frozen v0.3.0 evidence in all 180
  cells (`scripts/compare_benchmark.py`). See [CHANGELOG.md](CHANGELOG.md) for the list and
  [docs/SECURITY_MODEL.md](docs/SECURITY_MODEL.md) for what remains open.
* **0.4.0 (tag `v0.4.0`; first release in this repository).** Adds differential testing, bounded schedule exploration,
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

| Metric | **v0.3.0 benchmark (current)**: `bench-eed0c47427`, [docs/RESULTS-v0.3.0.md](docs/RESULTS-v0.3.0.md) | v0.2.0 (historical): `bench-7c4395ec2b`, [docs/RESULTS-v0.2.0.md](docs/RESULTS-v0.2.0.md) | v0.1.0 (historical): `bench-f5316f9d4b`, [docs/RESULTS.md](docs/RESULTS.md) |
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
scenario, oracle rule or recorded result. Beyond synthetic fixtures, they have been applied to one
AI-generated candidate, once, as a demonstration ([research/gemini-attempt2-results.md](research/gemini-attempt2-results.md)):

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
protocol in [docs/AGENT_EVALUATION_PROTOCOL.md](docs/AGENT_EVALUATION_PROTOCOL.md). On the host only labelled
synthetic fixtures have been run; one Gemini-generated candidate has been evaluated once, inside a disconnected
VM ([research/gemini-attempt2-results.md](research/gemini-attempt2-results.md)). No agent study has been run.

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
                  RELEASE_v0.4.1, RELEASE_v0.4.0, THIRD_PARTY, agent_task/ (task bundle for a future agent study),
                  RESULTS-v0.3.0, RESULTS-v0.2.0, RESULTS (v0.1.0), GAUNTLET_VALIDATION_AUDIT (v0.1.0 audit)
research/         technical report, results summary, demo script and transcript, open questions
evidence/         frozen benchmark databases with SHA-256 checksums (v0.3.0, plus historical v0.2.0 and
                  v0.1.0) and the supplementary v0.3.0 conventional re-run record
audit/            scripts used by the validation audit (see audit/PROVENANCE.md)
scripts/          run_demo.sh (one-command demonstration), compare_benchmark.py (re-execution vs frozen evidence)
.github/          continuous-integration workflow
docker-compose.yml   PostgreSQL 16 service (alternative to the local cluster; used by the Linux CI jobs)
PROJECT_SPEC.md      full specification + implementation status
CHANGELOG.md  SECURITY.md  CONTRIBUTING.md  CODE_OF_CONDUCT.md  CITATION.cff  LICENSE  NOTICE
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

Docker was not available on the development machine. This path is exercised by the Linux continuous-integration
jobs, which start PostgreSQL 16 from this file and run the full test suite against it.

### Any other PostgreSQL 16 server

Set `GAUNTLET_PG_DSN=postgresql://user:pass@host:port/postgres`. The user needs `CREATEDB`, because each
run creates and drops its own database, and `CREATEROLE` for external candidates: the harness connects on
the candidate's behalf as a per-run restricted role, and reads snapshots as a per-run observer role. A
non-superuser admin has not been tested.

## Configuration

GAUNTLET is configured with environment variables. All of them are optional.

| Variable | Default | Purpose |
|---|---|---|
| `GAUNTLET_PG_DSN` | `postgresql://gauntlet:gauntlet@127.0.0.1:55432/postgres` | Administrator connection to the PostgreSQL 16 server. The default password is a published development default, not a secret; use it only for a server bound to `127.0.0.1` |
| `GAUNTLET_HOME` | `<repository>/.gauntlet` | Working directory: local PostgreSQL cluster, results database, candidate snapshots (git-ignored) |
| `GAUNTLET_RESULTS_DB` | `$GAUNTLET_HOME/results.sqlite3` | SQLite file for run records. Every command adds missing tables to the file it opens, so **never point it at a file in `evidence/`**; work on a copy |
| `GAUNTLET_CANDIDATES_DIR` | `$GAUNTLET_HOME/candidates` | Where registered candidate snapshots are stored |
| `GAUNTLET_PG_BIN` | portable binaries under `$GAUNTLET_HOME/pgdist` | An existing PostgreSQL 16 `bin/` directory for `gauntlet pg init/start/stop` |
| `GAUNTLET_STEP_TIMEOUT_S` | `20` | How long one granted statement may run without completing or blocking on a lock |
| `GAUNTLET_STEP_CANCEL_GRACE_S`, `GAUNTLET_STEP_STALL_GAP_S` | `5`, `2` | Evidence thresholds for attributing a step timeout to a candidate ([docs/SECURITY_MODEL.md](docs/SECURITY_MODEL.md)) |
| `GAUNTLET_LOCK_TIMEOUT_MS`, `GAUNTLET_MONITOR_LOCK_TIMEOUT_MS` | `15000` | Lock-wait bounds for actor and harness connections |
| `GAUNTLET_MAX_RESULT_ROWS`, `GAUNTLET_MAX_RESULT_BYTES` | `100000`, 16 MiB | Largest result one statement may return to an actor |
| `GAUNTLET_ACTOR_CONNECT_TIMEOUT_S` | `10` | Bound on opening an actor's database connection |
| `GAUNTLET_MAX_CANDIDATE_RUNS` | `2` | External-candidate runs that may execute at the same time |
| `GAUNTLET_API_ALLOWED_HOSTS`, `GAUNTLET_API_ALLOWED_ORIGINS` | none | Extra `Host` names and dashboard origins the API accepts besides the loopback defaults |

The timeouts are safety nets, not part of the logical schedule: a run that hits one is recorded as an
infrastructure error, a timeout or a candidate failure, never as a pass.

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

Version 0.4.1, verified on 2026-11-13 (author-run; not independently reproduced):

| Where | Result |
|---|---|
| Windows 11, Python 3.14.0, PostgreSQL 16.14 (development machine) | **585 passed, 0 failed, 0 skipped** (`python -m pytest`, about 12 min) |
| Windows 11, Python 3.11.18, PostgreSQL 16.14 (development machine) | **585 passed, 0 failed, 0 skipped** |
| CI: Windows Server 2025, Python 3.14, PostgreSQL 16.14 | **585 passed, 0 skipped**; demonstration script 9 of 9 |
| CI: Ubuntu 26.04.1, Python 3.11 and 3.14, PostgreSQL 16.16 from `docker-compose.yml` | **579 passed, 6 skipped** on each Python version; demonstration script 9 of 9 |
| Dashboard | `npm run typecheck` and `npm run build` pass; `npm audit` reports 0 vulnerabilities |

On Linux, six tests that check Windows job-object limits are skipped by design, with the reason printed.
The first CI runs on Linux and on Python 3.11, on an earlier build of this release, found two defects, both
fixed in 0.4.1: the package could not be imported on Python 3.11, and the actor-protocol reader accepted
JSON values that were not objects (see [CHANGELOG.md](CHANGELOG.md)). Nothing has been run on macOS.

Before 0.4.1, full runs on the development machine ended with one failure (510 of 511, then 511 of 512). It was the
`< 10 s` wall-clock bound in `test_row_and_byte_budgets_stop_the_statement_without_buffering_it` (audit
finding FR-9, previously attributed to disk load). Its query put `generate_series` in `FROM`, which
PostgreSQL materializes into a 668 MB temporary file before returning the first row; that took about 11 s
on an idle machine, so the bound measured the disk and not the harness's result budget. The query now
streams its rows (0.2 s); the bound and the assertions are unchanged. Other tests still contain wall-clock
bounds and can fail on a heavily loaded machine. A machine that goes to sleep during the run also causes
failures: cleanup connections time out across the suspension, and the leak checks then report a test
database that was not dropped. Keep the machine awake for the 12 to 15 minutes the suite takes.

The tagged v0.4.0 release had 409 tests (67 need PostgreSQL). The numbers below describe the frozen v0.3.0
release. A step-by-step demonstration is in [docs/DEMO.md](docs/DEMO.md).

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

Both servers listen on the loopback interface only and have no authentication; keep it that way (do not
pass `--host`). The development server sends no CORS headers and serves only the dashboard's own files,
so other web pages open in your browser cannot read files or API data through it
([docs/SECURITY_MODEL.md](docs/SECURITY_MODEL.md#the-api-and-dashboard)).

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

## API

`python -m gauntlet serve` starts a local HTTP API on `http://127.0.0.1:8000`; interactive documentation
generated from the code is at `http://127.0.0.1:8000/docs`. Runs and analyses are queued and executed one
at a time; a `POST` returns an id immediately and you poll for the result.

```bash
curl http://127.0.0.1:8000/api/health
curl -X POST http://127.0.0.1:8000/api/runs -H "Content-Type: application/json" \
     -d '{"scenario_id": "S02_backfill_vs_legacy_writes", "impl_id": "m1_stale_backfill", "seed": 1}'
# -> {"id": "<run_id>", "status": "queued"}
curl http://127.0.0.1:8000/api/runs/<run_id>               # status, verdict, hashes, metrics
curl http://127.0.0.1:8000/api/runs/<run_id>/invariants    # the 12 invariant results with evidence
curl http://127.0.0.1:8000/api/runs/<run_id>/report        # Markdown report
curl -X POST http://127.0.0.1:8000/api/differential -H "Content-Type: application/json" \
     -d '{"impl_ids": ["ref_a_trigger", "m1_stale_backfill"], "scenario_id": "S02_backfill_vs_legacy_writes", "seed": 1}'
curl http://127.0.0.1:8000/api/differential/<comparison_id>
```

| Area | Endpoints |
|---|---|
| Catalog | `GET /api/meta`, `/api/scenarios`, `/api/implementations`, `/api/invariants`, `/api/faults`, `/api/fixtures` |
| Runs | `POST /api/runs`, `GET /api/runs`, `GET /api/runs/{id}` (`/trace`, `/invariants`, `/report`), `POST /api/runs/{id}/replay`, `GET /api/compare?a=&b=` |
| Analyses | `POST` and `GET` on `/api/differential`, `/api/explorations`, `/api/minimizations`; `POST /api/analyses/{id}/cancel` |
| Experiments | `POST /api/experiments`, `POST /api/experiments/{id}/report`, `GET /api/experiments/{id}/verify`, `/export` |
| Candidates | `GET /api/candidates`, `GET /api/isolation`, `POST /api/evaluations`, `GET /api/evaluations/{id}` |

The API has **no authentication** and is for local use only. It accepts only requests addressed to a
loopback name, refuses state-changing requests from other web origins, and requires JSON bodies
([docs/SECURITY_MODEL.md](docs/SECURITY_MODEL.md#the-api-and-dashboard)). Candidates cannot be registered
through the API; that is a command-line operation.

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

* No study of an external AI coding agent (Gemini, Claude or any other) has been run. One candidate generated
  with Gemini was evaluated once, inside a disconnected VM, as a demonstration: it failed one of six scenarios
  (`C1_COMPATIBILITY` under premature finalization) ([research/gemini-attempt2-results.md](research/gemini-attempt2-results.md)). One attempt says nothing about how often a
  model produces such a defect. All other validation uses hand-written references, hand-written mutants and
  labelled synthetic candidate fixtures.
* The candidate pipeline exists, but no execution mode is a security boundary. Candidate code in
  `process` mode can read host files (including the oracle), switch off the Python-level guard (plain
  Python suffices; the guard is a tripwire for trusted code, not a control), read the harness's
  environment or memory (same OS user), open network connections, and open its own database connection.
  Withholding credentials from the actor does not prevent this. `strict` (container/VM) isolation is not
  implemented.
* Audit finding A-02 is addressed since 0.4.1: an external candidate's database is observed by a
  per-run non-superuser role, never by a superuser session. The harness still holds superuser
  credentials (control connection, provisioning), which native candidate code can read in `process` mode.
* A candidate statement that runs past the step timeout is classified `candidate_statement_timeout` only
  when the harness's evidence supports it; ambiguous cases stay infrastructure errors. Neither is ever a
  pass, and the evidence rule is a heuristic (see the security model).

  Details: [docs/SECURITY_MODEL.md](docs/SECURITY_MODEL.md).
* Interleavings are explored at SQL-statement granularity: scripted or seeded-random in the benchmark,
  and exhaustively only within one interleave directive and a preemption bound (default 1) in
  exploration.
* The diagnoses S1/S2/S4/S6 mean "consistent with", not "root cause".
* The conventional baseline is one small suite written by the same author.
* All validation (benchmarks, reproducibility checks, tests, the self-audit) was run by the author. None
  of it has been independently reproduced.
* One conventional test in the v0.3.0 benchmark (m2, CT03) ended in an infrastructure error whose cause
  is unknown.
* Linux, Docker Compose and Python 3.11 are verified only by the continuous-integration run, not by a
  person; macOS has not been run.
* Each run takes about 4–10 s on Windows, dominated by process start-up.

Full list: [docs/LIMITATIONS.md](docs/LIMITATIONS.md).

## Roadmap

None of the following is implemented, and no dates are set.

1. **Measure instead of demonstrate.** Freeze a protocol (prompts, models, number of attempts, seeds,
   analysis plan) before collecting data; evaluate many independently generated migrations under identical
   conditions; report failure rates per invariant with confidence intervals; publish the raw evidence. The
   protocol draft is [docs/AGENT_EVALUATION_PROTOCOL.md](docs/AGENT_EVALUATION_PROTOCOL.md).
2. **Test the blind-spot claim on external code.** Run the conventional suite on external candidates, so
   that "passes ordinary tests, fails adversarial runs" can be measured for them too.
3. **A real isolation boundary.** Implement `strict` mode against a container or VM and test it with
   deliberately hostile fixtures ([docs/SECURITY_MODEL.md](docs/SECURITY_MODEL.md#what-must-exist-before-evaluating-untrusted-ai-generated-code)).
   Until then, every untrusted candidate needs its own disconnected VM.
4. **Wider search.** Exploration over more than one interleave directive and over crash points; detection
   as a function of budget.
5. **More tasks and independent scenarios.** New task families beyond the one money-column migration, and
   scenarios and faulty variants written by someone other than the author.

## Contributing, licence and citation

* Contributions: see [CONTRIBUTING.md](CONTRIBUTING.md) and the [code of conduct](CODE_OF_CONDUCT.md).
  Independent reproduction reports are the most valuable contribution.
* Security: see [SECURITY.md](SECURITY.md). Report vulnerabilities privately.
* Changes: see [CHANGELOG.md](CHANGELOG.md).
* Licence: [Apache License 2.0](LICENSE); copyright notice in [NOTICE](NOTICE). It covers the whole
  repository, including the frozen evidence and results records. The repository contains no third-party
  source code. The software it depends on is installed separately and keeps its own licences, among them
  the LGPL-3.0 PostgreSQL driver psycopg: see [docs/THIRD_PARTY.md](docs/THIRD_PARTY.md).
* Citation: see [CITATION.cff](CITATION.cff).
