# GAUNTLET: adversarial evaluation of database-migration code under concurrency, partial failure and retries

Technical report, software version 0.4.0 (proposed), benchmark version 0.3.0. October 2026.
Author-run research prototype; not independently reproduced or peer reviewed.

## Abstract

Code that migrates a live database must stay correct while old and new application versions write
concurrently, processes crash between transactions, responses are lost and clients retry. Conventional
functional tests rarely exercise these conditions and cannot reproduce them on demand. GAUNTLET is an
instrument for evaluating migration implementations under such adversarial execution. A single-threaded
controller gates every SQL statement of every actor process, so interleavings, crashes and lost responses
occur at exact statement boundaries and every run is replayable. An oracle that imports no
implementation code judges each run against twelve safety, recovery and compatibility invariants, using
the client-visible ledger and raw database snapshots, including a per-order linearizability check in
exact integer cents. On one task family (migrating a monetary column from `NUMERIC(12,2)` to `BIGINT`
cents on PostgreSQL 16), the frozen v0.3.0 benchmark of 180 runs shows that the oracle passes two
hand-written reference strategies on every run and kills each of eight hand-written single-point mutants
with its intended invariant. Four of the mutants pass the full conventional suite, and every re-run
reproduced its schedule, trace and outcome hashes. The prototype adds differential testing, bounded
systematic schedule exploration, failure minimization and experiment reports with hashed evidence
manifests, plus a pipeline for external candidates. No AI coding agent has been evaluated, and no
execution mode is yet a security boundary for untrusted code.

## 1. Problem statement

An online schema migration (expand → coexist → backfill → drain and finalize → contract) is a
distributed protocol between the legacy application, the new application and the migrator, mediated by
the database. Its correctness depends on how their statements interleave and on what happens when any of
them fails partway. Typical defects include:
* a backfill that writes a value computed from a stale read;
* a dual write split across two transactions;
* a retry that is applied twice;
* a contract step that runs while legacy instances still serve traffic.

All of these are invisible to sequential, single-client, crash-free tests.

## 2. Research motivation

AI coding agents are increasingly asked to write exactly this kind of infrastructure code, and they are
typically judged by whether it passes tests. The target question is whether agents produce code that
passes conventional tests but violates safety or recovery guarantees under adversarial conditions
(hypothesis H3 in [../docs/RESEARCH_DESIGN.md](../docs/RESEARCH_DESIGN.md)). Answering it requires an
instrument that is controllable, reproducible and judged independently of the code under test. This
report describes and validates that instrument. It does **not** answer the agent question.

## 3. System architecture

* **Task.** Migrate `orders.total NUMERIC(12,2)` to `orders.total_cents BIGINT` while a harness-owned
  legacy app (V1) and the implementation's new app (V2) share one PostgreSQL 16 database, through stages
  P0 → P5. An implementation provides V2 operations and migration steps through a fixed contract.
* **Engine.** Each actor runs in its own OS process with its own database session. Every statement
  waits at a gate; the controller grants statements according to a schedule document of directives:
  * `call`, `run`, `run_until(before_write | before_commit | after_commit)`;
  * `crash`, `restart`, `lose_response`, `retry`;
  * `interleave`, `drain`, `repeat`, `snapshot`.

  Lock waits are detected with `pg_blocking_pids()`, not timeouts. Logical sequence numbers, not wall
  time, order events.
* **Oracle.** It reads only:
  * the append-only client ledger (for each attempt: ok, failed, or uncertain);
  * raw snapshots taken on a separate monitor connection;
  * the initial state;
  * the strategy's declared agreement contract.

  It never reads implementation code or trusts implementation-reported state.
* **Storage and analyses.** Runs are stored in SQLite with schedule, trace and outcome hashes. Differential
  testing, exploration, minimization and experiment reports operate on stored runs. A FastAPI backend and
  a React dashboard expose everything.

Details: [../docs/ARCHITECTURE.md](../docs/ARCHITECTURE.md).

## 4. Experimental methodology

* **Implementations.** Two reference strategies establish that each scenario is satisfiable:
  * A, trigger synchronisation;
  * B, atomic dual-write plus post-drain reconciliation.

  Eight mutants each change one aspect of a reference and have a documented intended failure.
* **Scenarios.** Six scenarios (S01–S06), each a directive template with fixed fault opportunities: the
  baseline migration, backfill racing legacy writes, a lost acknowledgement with retry, crash and resume,
  premature finalization, and unbackfilled records. Seeds determine:
  * data and amounts;
  * operation targets;
  * statement-level interleavings;
  * per scenario, batch sizes, crash positions and episode order.
* **Benchmark.** Every implementation runs every scenario with seeds 1–3, giving 180 runs. 60 cells are
  re-run to check reproducibility, and the 10-test conventional suite is run per implementation.
  Inconclusive and infrastructure-error runs are never counted as passes or detections.
* **Analyses.**
  * Differential testing: same schedule, normalized comparison, four classifications.
  * Exploration: a preemption-bounded DFS over one interleave directive.
  * Minimization: ddmin over schedule units, with failure-signature matching.
  * Experiment reports: metric definitions and a hashed manifest.

Full description: [../docs/RESEARCH_METHODOLOGY.md](../docs/RESEARCH_METHODOLOGY.md).

## 5. Invariants and failure model

**Twelve invariants.**
* Safety:
  * S7, per-order linearizability of the client-visible history (the core check);
  * S1, acknowledged writes are durable;
  * S2, exact cents;
  * S3, representation agreement under the declared contract;
  * S4, no duplication;
  * S5, records preserved;
  * S6, failed operations are not applied.
* Recovery: R1 resume after interruption; R2 repeat safety; R3 backfill retry safety; R4 a bounded
  terminal-state check.
* Compatibility: C1, live old versions are not broken.

When S7 fails, the oracle searches relaxed models (lost acknowledgement, duplication, failed-but-applied,
inexact cents) to say what the violation is *consistent with*. It does not claim a root cause.

**Fault model.** Arbitrary statement-level interleavings, process kills at statement boundaries, dropped
responses after commit, client retries with the same idempotency key, and repeated or resumed migration
steps.

**Not modelled.** Faults inside a statement, network partitions, disk faults, clock skew, and isolation
levels other than READ COMMITTED.

## 6. Reproducible synthetic results

From the frozen v0.3.0 benchmark (`bench-eed0c47427`). The totals were recomputed from the raw run rows
during the release review.

| Metric | Value |
|---|---|
| Runs | 180 = 6 scenarios × 10 implementations × 3 seeds |
| Verdicts | 121 pass, 59 fail (confirmed invariant violations), 0 inconclusive, 0 infrastructure errors |
| Invariant-level FAIL entries | 108 (violated run–invariant pairs, not distinct defects) |
| References | 36/36 pass |
| Mutants killed by intended invariant | 8/8 |
| Reproducibility | 60/60 re-runs identical in schedule, trace, outcome hash and verdict |
| Mutants that pass all conventional tests but fail adversarially | 4 (m1, m3, m7, m8); m2 inconclusive (conventional CT03 infrastructure error, cause unknown) |

From the synthetic demonstration, re-run during the release review:
* **Differential testing.** Reference vs m1 is a *confirmed violation*. The two references are in
  *agreement*.
* **Exploration.** For the F01 fixture with m1, all 18 schedules with at most one preemption were
  executed: 5 failed, all in one failure class, and the representative replayed.
* **Minimization.** 104 → 19 directives, 1-minimal with respect to unit deletion.
* **Synthetic candidate.** The external-candidate evaluation of a synthetic fixture completed, and its
  evidence manifest verified 5/5 artifacts unchanged.

See [results-summary.md](results-summary.md).

## 7. Example failure and minimized schedule

Mutant `m1_stale_backfill` computes `total_cents` in application code from a value it read earlier,
instead of inside the `UPDATE`. In scenario S02 (seed 1), the backfill races legacy writes. The original
schedule has 104 directives, 54 operations and 390 trace events. Minimization keeps 3 operations:

```text
P0  spawn v1-a, v1-b, migrator
P1  migrator: expand                           (L001)
    spawn v2-a
P3  migrator: backfill_batch(batch_size=4)     (L002)  run until before its first write   [fault F3]
    v1-b:     add_charge(order 4, 5.04)        (L004)  runs to completion, acknowledged
    migrator: continues and writes cents computed from the value read before L004
P4  drain v1-a, v1-b
P5  contract
```

The oracle reports **S1_ACKED_DURABLE FAIL** on order 4, consistent with `lost_ack`: the acknowledged
legacy charge is overwritten by the stale backfill value. S7_LINEARIZABLE fails too: no order of the
operations explains the final total.
* No single-unit removal from these three operations reproduces the same failure signature.
* The minimized schedule replays with an identical signature, trace and outcome.
* The original run record is unchanged.

The same race is found independently by exploration of fixture F01: 5 of its 18 bounded schedules fail
in this failure class.

## 8. Threats to validity

* **Authorship.** One author wrote the oracle, scenarios, references, mutants and conventional suite. A
  misunderstanding shared by the oracle and the references would go undetected, and scenarios may be
  shaped around the mutants.
* **Oracle permissiveness.** Uncertain operations may take effect anywhere in the union of their
  attempts' windows. This can hide violations, but it cannot invent them.
* **Coverage.** Benchmark schedules are scripted or seeded-random. Exploration is exhaustive only within
  one directive and one preemption. Detection is schedule-dependent in places.
* **Baseline.** The conventional suite is one small sequential suite. Better sequential tests could catch
  m3, m7 and m8; only m1 and m2 strictly need interleaving control or fault injection.
* **External validity.** One task family, one database version, one machine, statement granularity.
* **Reproduction.** All validation was author-run. An infrastructure error in m2's CT03 remains
  unexplained.

## 9. Security limitations

* **`process` mode.** This is a development sandbox for trusted code:
  * a Windows Job Object (process count, memory and CPU limits);
  * an allow-listed environment and a per-actor workspace;
  * a Python audit-hook guard;
  * a per-run restricted PostgreSQL role, with access to other runs' databases revoked.

  It does **not** prevent reading host files, including the oracle, opening network connections from
  native code, or bypassing the guard.
* **`strict` (container/VM) mode** is not implemented and fails closed. Without an explicit
  trusted-environment declaration, every entry point refuses to load a candidate.

Untrusted, AI-generated code must not be run until a verified boundary exists:
[../docs/SECURITY_MODEL.md](../docs/SECURITY_MODEL.md).

## 10. Open research questions

* Independent reproduction.
* Safe evaluation of real agents.
* Oracle validation: an independent S7 implementation, and oracle mutation testing.
* Independently authored mutants and scenarios.
* Broader exploration: several directives, crash points and partial-order reduction.
* Comparison with existing approaches (none run; no novelty claimed).

See [open-questions.md](open-questions.md).

## 11. Next experiments

1. External reproduction of the frozen evidence check, the test suite and the demonstration on Linux.
2. A container or VM boundary for `strict` mode, verified with hostile fixtures and positive controls.
3. A pilot agent study under [../docs/AGENT_EVALUATION_PROTOCOL.md](../docs/AGENT_EVALUATION_PROTOCOL.md),
   only after step 2: several attempts per agent, reporting confirmed violations, inconclusive results and
   infrastructure errors separately.

## References

None are cited. The related-work topics in
[../docs/RESEARCH_DESIGN.md](../docs/RESEARCH_DESIGN.md#related-work-to-be-reviewed-before-claiming-novelty)
must be reviewed, and their citations verified, before any comparison or novelty claim is made.
