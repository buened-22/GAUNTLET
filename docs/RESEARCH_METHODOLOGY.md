# Research methodology

This is the single entry point to how GAUNTLET's experiments are designed, executed and verified. It
summarizes and links the detailed documents; where they differ, the detailed document and the code are
authoritative. Hypotheses and their measurement status are in [RESEARCH_DESIGN.md](RESEARCH_DESIGN.md).

## 1. Research question and threat model

> Can AI coding agents produce software that passes conventional functional tests but violates safety or
> recovery guarantees under unseen combinations of concurrency, partial failure, retries and interrupted
> execution?

This version does **not** answer the question: no study of an AI agent has been run (one
Gemini-generated candidate has been evaluated once, as a demonstration:
[../research/gemini-attempt2-results.md](../research/gemini-attempt2-results.md)). It builds and validates
the *instrument*: a precisely specified task, a controllable and reproducible adversarial execution
engine, an oracle independent of the implementation under test, and evidence that the instrument
separates correct from incorrect implementations for the intended reasons.

*Fault model* (what the environment may do to the system under test): interleave statements of
concurrent clients arbitrarily at SQL-statement granularity; kill an actor process at any statement
boundary (before a write, before or after commit); drop a response after the server committed; make the
client retry with the same idempotency key; repeat or resume a migration step; keep the legacy version
running while the new version and the migrator act. Network partitions, disk faults, clock skew and
faults inside a statement are not modelled.

The security threat model for executing candidate code is separate: see
[SECURITY_MODEL.md](SECURITY_MODEL.md).

## 2. System under test and assumptions

One task family: migrate `orders.total NUMERIC(12,2)` to `orders.total_cents BIGINT`, exact to the cent,
while a legacy application version (V1, harness-owned) and a new version (V2) share one database, through
stages P0 legacy → P1 expand → P2 coexist → P3 backfill → P4 drain and finalize → P5 contract. An
implementation supplies the V2 application operations and the migration steps through the contract in
`backend/gauntlet/impls/base.py`.

Assumptions: PostgreSQL 16 (all recorded results: 16.14, portable local cluster), READ COMMITTED,
`lock_timeout = 15s` per actor session, one database per run, each actor (V1 app, V2 app, migrator) in
its own OS process. Python ≥ 3.11 (recorded results: 3.14.0, Windows 11).

## 3. Invariants and outcomes

Twelve invariants. The oracle ([INVARIANTS.md](INVARIANTS.md), `backend/gauntlet/oracle/`) imports no implementation code.
Its inputs are the harness's append-only client ledger (what each client sent and whether it received
success, an explicit error, or nothing), raw database snapshots read on a separate monitor connection,
the schedule's initial state, and the strategy's declared agreement contract.

| Group | Invariants |
|---|---|
| Safety | S7 client-visible history is linearizable per order (core check); S1 acknowledged writes are not silently lost; S2 exact cents; S3 representations agree where both are authoritative; S4 retries do not duplicate operations; S5 records preserved; S6 failed operations are not reported as committed |
| Recovery | R1 interrupted steps resume; R2 repeating a completed step is harmless; R3 backfill retry is safe; R4 the migration reaches a valid terminal state (bounded) |
| Compatibility | C1 live old versions are not broken |

Per invariant: `pass`, `fail`, `inconclusive`, `not_applicable`, `infra_error`. Run verdict, first rule
that applies: `infra_error` if any infrastructure error (nothing is evaluated); `fail` if any invariant
FAILs; `inconclusive` if any invariant is inconclusive, the engine recorded an incomplete condition, or
nothing actually passed; otherwise `pass`. `not_applicable` never counts as a pass.

The terms used in results are kept distinct:

| Term | Meaning |
|---|---|
| **Confirmed invariant violation** | the oracle reports FAIL for an invariant in a run with no infrastructure error |
| **Failing implementation** (for a set of runs) | at least one of its runs has a confirmed violation |
| **Differential disagreement** | two implementations' normalized behaviour differs on the same schedule; by itself it says nothing about correctness |
| **Inconclusive** | the oracle could not decide (or a differential pair contains an inconclusive run) |
| **Infrastructure error** | the harness, database or execution failed; never a pass and never a failure of the implementation |

Diagnoses attached to S1, S2, S4 and S6 evidence ("consistent with a lost write", ...) come from relaxed
models; they mean *consistent with*, not *root cause*.

## 4. References and mutants

Two reference strategies establish that each scenario is satisfiable: A, trigger synchronisation
(`ref_a_trigger`); B, atomic dual-write plus post-drain reconciliation (`ref_b_dualwrite`)
([ARCHITECTURE.md](ARCHITECTURE.md)). If a reference fails a scenario, the scenario or the oracle is
wrong. They are different designs, but they were written by the same author as the oracle, scenarios and
mutants and share the harness-owned V1 code and contract, so a misunderstanding common to all of them
would not be detected (see §10).

Eight single-point mutants, each derived from a reference by one documented change with an intended
failing invariant and scenario ([FAULT_CATALOG.md](FAULT_CATALOG.md)): m1 stale backfill (S1), m2
non-atomic dual write (S3), m3 retry duplicates (S4), m4 premature contract (C1), m5 backfill skips a row
(R4), m6 float cents (S2), m7 assumes backfilled (S7), m8 skips reconciliation (S1). A mutant is *killed
as intended* when its intended invariant FAILs in at least one run of an intended scenario with no
infrastructure error. Mutants are plausible defects, not a sample of the defects agents make.

## 5. Differential testing

Two or more implementations execute the identical schedule document, each on a fresh database.
Client-visible behaviour and final state are normalized by six documented rules (N1–N6: timing and OS ids
dropped, errors compared by code, migration-step payloads compared by status only, client results
compared exactly, final snapshot compared exactly, attempts matched by id) and compared pairwise.
Classification: `inconclusive` (any execution error, or a difference with only inconclusive oracle
results), `confirmed_violation` (difference and an oracle FAIL in some run), `unconfirmed_disagreement`
(difference, every run passes the oracle; may be harmless), `agreement` (no normalized difference).
**Agreement is not correctness**: if the oracle FAILs in agreeing runs, the shared violation is flagged.
The oracle judges each run on its own. Details: [ANALYSES.md](ANALYSES.md).

## 6. Schedule exploration and its bounds

Preemption-bounded, stateless depth-first search with re-execution over **one** `interleave` directive.
The root is the deterministic non-preemptive schedule; children replace one decision with another
runnable actor and continue non-preemptively; children over `max_preemptions` (default 1) are not
generated. No partial-order reduction. Each executed schedule is identified by its realized decision
sequence; a duplicate (possible only under nondeterminism) is never expanded and makes the search
`incomplete`.

Claim, only when the status is `completed`: every schedule of that directive with at most
`max_preemptions` preemptions was executed, assuming deterministic execution (failure-class
representatives are replayed to check it). Crash points, other directives and schedules with more
preemptions are **not** explored. "18 schedules" for `F01_backfill_race_probe` means 18 distinct realized
interleavings within that bound, not repeated executions. Failure classes group failing schedules by the
set of FAILed invariants and diagnosis categories (symptoms, not root causes).

## 7. Failure minimization

Delta debugging (ddmin) over removable *schedule units* of a saved failing run (an operation with its
dependent directives, a snapshot, a repeat, ...). The original must reproduce on every one of
`reproduce_repeats` executions first, otherwise the result is `flaky` or `not_reproducible`. A candidate
schedule is accepted only if the target invariant FAILs with a compatible *failure signature* (same
invariant; diagnosis categories a non-empty subset of the original's; overlapping affected orders,
failing operations and error codes), confirmed `confirm_repeats` times. Infrastructure errors, timeouts
and other violated invariants never count as reproducing.

The result is *1-minimal with respect to unit deletion* only when every single-unit removal of the final
schedule was executed and rejected on its evidence; a removal that ended in an infrastructure error or
timeout, or that the budget cut off, voids the claim. No global minimality is claimed and batch sizes or
row counts are not reduced. The minimized schedule is replayed independently and the original run record
is hashed before and after (`original_unchanged`).

## 8. Replay and evidence integrity

* **Run identity.** `schedule_hash` (the schedule document, including the benchmark version),
  `trace_hash` (the statement-level event trace without timing and OS ids) and `outcome_hash` (ledger
  outcomes, snapshots and invariant results). `python -m gauntlet replay <run>` re-executes a run's
  configuration and compares all three plus the verdict.
* **Benchmark reproducibility.** Each benchmark re-runs 60 of its 180 cells and requires identical
  schedule, trace, outcome and verdict.
* **Frozen evidence.** Benchmark databases in `evidence/` with SHA-256 files; verify with
  `(cd evidence && sha256sum -c *.sha256)`.
* **Experiment manifests.** An experiment report hashes every run record, benchmark summary and analysis it
  used; `GET /api/experiments/<id>/verify` re-hashes them. This shows the evidence is present and
  unchanged since the report; it does not by itself show the report's numbers were derived correctly. The
  markdown hash is recorded, and regenerating a report from the stored configuration and evidence
  reproduces it byte for byte (checked during the v0.4.0 release review). The manifest is stored in the same
  database, so it detects accidental change, not deliberate tampering.
* **Candidates.** Content-addressed snapshots; `python -m gauntlet candidate verify <id>`.
* **Provenance.** Every run records the harness source fingerprint (all `gauntlet/**/*.py`, line endings
  normalized), oracle and engine fingerprints, git revision and whether the working tree differs,
  PostgreSQL, Python and package versions.

## 9. Configuration and reproduction

The v0.3.0 benchmark (`bench-eed0c47427`): benchmark version 0.3.0, 6 scenarios (S01–S06) × 10
implementations × seeds 1–3 = 180 runs, plus 60 reproducibility re-runs and the conventional suite, on
PostgreSQL 16.14, Python 3.14.0, Windows 11. Seeds determine the initial data and amounts, operation
targets, statement-level interleavings and, per scenario, batch sizes, crash positions and episode order
([RESEARCH_DESIGN.md](RESEARCH_DESIGN.md#variables)); the same seed always rebuilds the same schedule.

Exact procedures: [REPRODUCIBILITY.md](REPRODUCIBILITY.md) (verifying the frozen evidence, re-running
the benchmark into a new database, the synthetic end-to-end demonstration).

## 10. Threats to validity, limitations and open questions

* **Authorship.** One author wrote the oracle, scenarios, references, mutants and conventional suite.
  Scenarios may be shaped around the mutants; a misunderstanding of the specification shared by oracle and
  references would be invisible. The only mitigations are black-box injection points, non-mutant-specific
  random interleavings and oracle unit tests on hand-built histories. Independent authoring and review are
  needed.
* **Oracle permissiveness.** Uncertain operations may take effect anywhere in the union of their attempts'
  windows; this can hide violations, never invent them.
* **Coverage.** Benchmark schedules are scripted or seeded-random; exploration is exhaustive only within one
  directive and one preemption. Passing is evidence about the executed schedules only.
* **Conventional baseline.** One small sequential suite by the same author; m3, m7 and m8 could be caught by
  additional sequential tests. Only m1 and m2 strictly need interleaving control or fault injection.
* **External validity.** One task family, one database, one machine, statement granularity.
* **Unexplained event.** Conventional test CT03 for m2 in the v0.3.0 benchmark ended in an infrastructure
  error of unknown cause (recorded as inconclusive).
* **No independent reproduction.** All validation was run by the author.
* **No agent study** (one Gemini-generated candidate evaluated once, as a demonstration); **no security
  boundary** for untrusted code in GAUNTLET itself.

Full lists: [LIMITATIONS.md](LIMITATIONS.md), [RESEARCH_DESIGN.md](RESEARCH_DESIGN.md#threats-to-validity)
and [../research/open-questions.md](../research/open-questions.md).
