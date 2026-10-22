# Project Gauntlet: specification

Part I is the original research and engineering specification, reproduced unchanged. Part II
records, requirement by requirement, what the prototype implements, how it does so, and what
remains. Part II tells you what exists; Part I describes the full intended system.

---

# Part I: Original specification

## Master Build Specification
### An Adversarial Evaluation Framework for AI-Engineered Software Systems

### 1. Research vision

Gauntlet evaluates whether software engineered by AI coding agents remains correct under adversarially controlled execution.

Central research question:

**Can AI coding agents produce software that passes conventional functional tests but violates safety or recovery guarantees under previously unseen combinations of concurrency, partial failure, retries, and interrupted execution?**

The initial prototype will focus on one narrowly defined, technically rigorous scenario: a database schema migration while old and new application versions coexist.

The architecture should eventually support additional task families, but do not attempt to implement a complete benchmark platform in the first version.

### 2. Non-negotiable engineering principles

1. Correctness must be checked independently of the implementation being evaluated.
2. Every benchmark run must be reproducible from a recorded seed and configuration.
3. Every reported failure must have supporting execution evidence.
4. The grader must distinguish expected failures from genuine infrastructure errors.
5. The system must never claim a test passed if it was skipped, not executed, or inconclusive.
6. Deliberately faulty implementations must fail for the intended reason, not because of unrelated setup errors.
7. Never fabricate experiment results, model rankings, performance improvements, or benchmark coverage.
8. Clearly distinguish implemented capabilities from planned capabilities.
9. Prefer a small, fully working system over a large, partially functional codebase.
10. Keep the code readable, modular, typed where appropriate, and documented for an engineer reviewing it independently.

### 3. Prototype scenario

Create a small order-management application.

The legacy schema contains `orders.id` and `orders.total NUMERIC(12,2)`. The new schema introduces `orders.total_cents BIGINT`. The migration must preserve the monetary value exactly to the cent.

The application has two versions: Version 1 reads and writes the legacy `total` column. Version 2 supports the new representation and must remain compatible with the migration protocol.

The system must support a migration through these stages:

- P0: Legacy application only.
- P1: Expand the schema while the legacy application continues serving requests.
- P2: Run old and new application versions concurrently.
- P3: Backfill existing records in bounded batches while new writes continue.
- P4: Drain old application instances and perform the final migration steps.
- P5: Operate using the new schema and verify the final state.

The prototype must exercise at least one meaningful failure scenario involving an interrupted migration, a retry, or concurrent writes.

Use PostgreSQL 16 for the database integration. Use Docker Compose for local infrastructure where available. If Docker is unavailable, provide an explicit setup alternative and report which integration tests could not run.

### 4. The central research component: Adversarial Execution Engine

Implement an engine that executes predefined operations according to a controlled event schedule. Operations should include: create an order; update an order; read an order; execute a migration step; backfill a bounded batch; interrupt a worker; restart a worker; retry an operation after a simulated timeout; switch between old and new application versions.

The engine must be able to control the ordering of events. Use a deterministic scheduler or event queue for the initial implementation.

Each run must record: random seed, if randomness is used; scenario identifier; schedule identifier; initial database state; ordered event trace; requests and their outcomes; acknowledged writes; transaction outcomes; injected faults; recovery actions; final invariant results; runtime and infrastructure errors.

Repeated runs using the same seed and configuration must reproduce the same logical schedule and outcome, assuming the environment and database version are unchanged. Do not claim complete determinism for uncontrolled operating-system scheduling or external infrastructure. If PostgreSQL concurrency introduces nondeterministic timing, separate the deterministic schedule definition from observed execution timing and record both.

### 5. Independent correctness oracle

Build a verification component independent of the implementation under evaluation. Maintain an append-only event ledger of operations acknowledged to clients. The oracle must not infer expected outcomes by querying the implementation's final totals and accepting them as ground truth.

The oracle should independently calculate expected order values and verify:

**Safety invariants:** acknowledged writes are not silently lost; monetary values remain exact to the cent; old and new representations agree wherever both are required to be authoritative; retries do not duplicate logical operations; a record does not disappear or become corrupted during migration; failed operations are not reported as successfully committed; schema transitions preserve all required records and values.

**Recovery invariants:** an interrupted migration can resume safely; repeating a completed migration step does not corrupt state; backfill operations can be retried safely; the system eventually reaches a valid terminal state under explicitly stated recovery assumptions.

Do not claim liveness or eventual recovery without defining the assumptions and time or step bounds under which they are evaluated. The oracle must return structured results, including invariant identifiers, pass or fail status, relevant evidence, and any inconclusive state.

### 6. Reference implementations

Build and test a known-correct baseline before evaluating faulty implementations. Provide two migration strategies where practical:

**Strategy A: Trigger-based synchronization.** Use a database trigger or equivalent controlled database mechanism to maintain consistency between the legacy and new representations during the transition.

**Strategy B: Atomic application dual-write.** The application writes both representations in the same database transaction. Include appropriate transaction handling and final reconciliation.

Document the correctness assumptions and tradeoffs of both strategies. Do not assume either strategy is correct simply because it was designed as a reference implementation. Prove it against the specified test cases. If the complete second strategy is too large for the first milestone, implement one correct strategy fully and make the second a clearly documented extension.

### 7. Fault injection and adversarial cases

Implement a small but meaningful initial fault suite, including cases such as: a worker crashes after a transaction commits but before the caller receives confirmation; a retry repeats an operation whose original outcome is uncertain; a legacy application instance writes during backfill; a backfill batch is interrupted and retried; a migration step is executed more than once; a new application instance encounters a record that has not yet been backfilled; a process is restarted during a migration stage; a schema constraint is introduced while older application instances still depend on the old schema.

Define each fault with: preconditions; injection point; expected system behavior; expected invariant outcomes; recovery obligations; a reproducible execution trace. A fault must be injected at a valid, controlled point. Do not simulate a crash by simply declaring a test failed. Where a true process crash is necessary, use a subprocess or another explicit process boundary. For the first prototype, prioritize four to six carefully engineered scenarios rather than a large collection of superficial tests.

### 8. Deliberately faulty implementations

Create a set of controlled faulty variants (mutants), for example: a migration that backfills records without accounting for concurrent legacy writes; a dual-write implementation that does not use one atomic transaction; a retry path that duplicates a logical operation; a finalization step that removes compatibility before old application instances have drained; a backfill that incorrectly skips a record; a migration that assumes every record has already been backfilled.

Each mutant must have a documented intended failure. Validate that the mutant fails because of the intended invariant violation, not because of an unrelated syntax error, unavailable service, or malformed test setup. If a mutant is killed by an unintended failure, fix the mutant or revise the test before reporting coverage. Do not make the faulty variants so trivial that they only test whether the harness can detect syntax errors.

### 9. Evaluation methodology

The prototype must distinguish: conventional functional test results; adversarial execution results; independent oracle results. Demonstrate at least one controlled example in which an implementation passes the conventional test suite but fails an adversarial scenario, if the implemented scenario genuinely exhibits that behavior. Do not manufacture such an outcome or hardcode a failure report to make the demonstration impressive.

Report: number of scenarios executed; number of runs completed; number of invariant violations; number of faulty variants detected; number of inconclusive runs; number of infrastructure failures; reproducibility results.

Do not create an overall agent ranking until multiple real agent runs have been collected under equivalent conditions. For the initial prototype, provide a reproducible command to run the benchmark against reference implementations and mutants. Later, the system should support evaluating solutions generated by external coding agents. Do not claim to have evaluated Claude, Gemini, or any other model unless an actual evaluation has been performed and recorded.

### 10. User interface

Build a polished, professional research dashboard for inspecting actual benchmark execution. Preferred stack: React, TypeScript, Vite, a consistent accessible component and styling system, FastAPI backend, Python benchmark engine, PostgreSQL 16, Docker Compose. Views: (A) Overview; (B) Run benchmark; (C) Execution timeline; (D) Invariant report; (E) Run comparison; (F) Scenario library; (G) Research report. Use actual backend data; label unexecuted metrics as unavailable; no fake progress; distinguish pass, fail, inconclusive and infrastructure error; avoid unnecessary animations, fake terminal output, decorative metrics and invented model-performance statistics.

### 11. Backend API and architecture

Clear module boundaries for scenario definitions, event scheduling, fault injection, application and migration operations, event ledger, independent oracle, mutant registry, benchmark execution, result persistence, API endpoints, report generation. Endpoints to list scenarios, start a run, inspect a run, retrieve its trace, inspect invariant results, and compare completed runs. Validate input and handle exceptions explicitly. Use migrations or reproducible database initialization scripts. Persist completed run metadata and results. Never execute arbitrary user-supplied code in the main application process.

### 12. Testing requirements

Automated tests for: monetary conversion and exact cent arithmetic; oracle correctness; event-ledger behavior; scheduler reproducibility; fault injection; reference implementation correctness; mutant detection; run persistence; API behavior; report generation; error handling. Integration tests for the PostgreSQL migration scenario. At least one test proving that the oracle detects a known incorrect final state even when the application itself reports success. At least one test showing that a failed or inconclusive run is not incorrectly reported as a pass. Run the tests and report the actual results. Do not modify tests merely to conceal an implementation defect.

### 13. Required documentation

`README.md`, `PROJECT_SPEC.md`, `docs/RESEARCH_DESIGN.md`, `docs/INVARIANTS.md`, `docs/FAULT_CATALOG.md`, `docs/ARCHITECTURE.md`, `docs/RESULTS.md` (from actual runs only), `docs/LIMITATIONS.md`.

### 14–16. Workflow, definition of done, final instructions

Work incrementally through six milestones (inspect and plan; core engine; adversarial scenarios; API and persistence; dashboard; validation and delivery). The prototype is complete when it can be started with documented commands, a user can initiate a benchmark from the UI or CLI, a real scenario executes, a reference passes, at least two mutants produce verified invariant violations for the intended reasons, the oracle checks results, traces are persisted and inspectable, the same configuration reproduces the same logical schedule, the dashboard reports actual backend results, tests have been executed with real outcomes documented, and limitations are disclosed. Prioritise correctness and evidence over feature count; make no novelty or superiority claims without review and fair comparison; never fabricate results.

---

# Part II: Implementation status (prototype v0.3.0)

**Version history**

* **v0.1.0.** The first implementation. Its benchmark `bench-f5316f9d4b` is historical evidence
  (`docs/RESULTS.md`, `evidence/results-bench-f5316f9d4b.sqlite3`). It is the subject of the self-audit
  `docs/GAUNTLET_VALIDATION_AUDIT.md`.
* **v0.2.0 (historical).** Fixes audit Finding 1 (the oracle identifies a logical write by its idempotency
  key) and Finding 2 (seeds vary schedule structure in S01, S03, S04 and S05, not only amounts). Benchmark
  `bench-7c4395ec2b` (`docs/RESULTS-v0.2.0.md`, `evidence/results-bench-7c4395ec2b.sqlite3`). The other
  audit findings, including Finding 3 (S1 diagnosis over-attribution), remain open.
* **v0.3.0 (current).** Two changes:
  * S04 repeats the completed `expand` a second time, after the backfill has populated `total_cents`, so
    R2 can observe a repeated `expand` that destroys stored values. The earlier repeat runs before any
    data exists.
  * Conventional-suite reporting keeps pass, fail and infrastructure error distinct; an infrastructure
    error is no longer reported as a test failure.

  Benchmark `bench-eed0c47427` (`docs/RESULTS-v0.3.0.md`, `evidence/results-bench-eed0c47427.sqlite3`).
  Compared with v0.2.0, every matrix cell has the same verdict and invariant statuses, and only the 30
  S04 cells differ, by the inserted directive and its direct consequences. In this benchmark, conventional
  test CT03 for m2 ended in an infrastructure error of unknown cause, so m2's conventional result is
  recorded as inconclusive. The frozen database is a copy of the original diagnostic database in which only
  the benchmark summary was recomputed by the corrected reporting code; all run rows are identical. A
  later supplementary conventional re-run (`evidence/conventional-rerun-bench-eed0c47427.json`) had no
  infrastructure errors. It is kept as a separate record and does not replace the benchmark's result. See
  `evidence/README-bench-eed0c47427.md`. Audit Finding 3 and the other open findings remain open.

All validation of every version was run by the author; none has been independently reproduced.

Legend:

* **Done**: implemented and exercised by tests or the benchmark.
* **Partial**: implemented with a stated restriction.
* **Planned**: not implemented.

| § | Requirement | Status | Where / how |
|---|---|---|---|
| 3 | Legacy and new schema, exact cents | Done | `pg/provision.py` (harness-owned P0 schema), `money.py` |
| 3 | V1 / V2 application versions | Done | `impls/legacy.py` (V1, identical for all strategies), V2 in each strategy |
| 3 | Stages P0–P5 | Done | `stage` directives; every scenario runs P0 to P5 |
| 3 | PostgreSQL 16 | Done | PostgreSQL 16.14 (portable binaries, `python -m gauntlet pg …`) |
| 3 | Docker Compose | Partial | `docker-compose.yml` provided; **untested** (no Docker on the development machine) |
| 4 | Controlled ordering of events | Done | Statement-level gates in actor subprocesses; single-threaded controller |
| 4 | Ops: create / update / read / migrate / backfill / interrupt / restart / retry / version switch | Done | Client ops; `expand`, `backfill_batch`, `finalize`, `contract`; `crash`, `restart`, `retry`, `lose_response`; v1/v2 actors and `drain` |
| 4 | Run record (seed, ids, initial state, trace, requests, ack'd writes, faults, recovery, invariants, errors) | Done | `engine/runner.py` run dict, persisted by `store.py` |
| 4 | Logical schedule separated from observed timing | Done | `schedule_hash` (definition) vs `trace_hash` (logical trace) vs `t_ms` (excluded) |
| 5 | Append-only ledger; oracle independent of implementation | Done | `oracle/ledger.py`, `oracle/checker.py` (no implementation imports) |
| 5 | Safety invariants | Done | S1–S7 |
| 5 | Recovery invariants with stated bounds | Done | R1–R4 (`docs/INVARIANTS.md`) |
| 5 | Structured results with evidence and inconclusive state | Done | per-invariant `status`, `summary`, `evidence`, `trigger_seq` |
| 6 | Strategy A (trigger) | Done | `impls/reference_a.py` |
| 6 | Strategy B (atomic dual-write + reconciliation) | Done | `impls/reference_b.py` |
| 7 | Eight faults with preconditions, injection point, expected outcome, recovery, trace | Done | `faults.py`, `docs/FAULT_CATALOG.md` |
| 7 | True process crash | Done | Every actor is an OS process; `crash` hard-kills it |
| 7 | Four to six scenarios | Done | Six scenarios (S01–S06) |
| 8 | Mutants with intended failures, validated | Done | 8 mutants (`impls/mutants.py`); validation in `bench.py` |
| 9 | Conventional, adversarial and oracle results distinguished | Done | `conventional.py`, engine trace, oracle results |
| 9 | Required metrics and reproducibility | Done | `bench.py` → `docs/RESULTS-v0.3.0.md` (current); `docs/RESULTS-v0.2.0.md` (historical v0.2.0); `docs/RESULTS.md` (historical v0.1.0); frozen databases in `evidence/` |
| 9 | External agent evaluation | Planned | Execution boundary exists (subprocess per actor); no agent solutions collected |
| 10 | Dashboard views A–G | Done | `frontend/src/pages/*` |
| 11 | API endpoints, validation, persistence | Done | `api/app.py`, `store.py` |
| 11 | No user-supplied code in the main process | Done | Only registered implementations; they run in subprocesses |
| 12 | Automated tests | Done | `backend/tests/` (unit + PostgreSQL integration + API) |
| 13 | Documentation | Done | this file, `README.md`, `docs/*` |
