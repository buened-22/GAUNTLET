# Fault catalog

Faults are injected by **engine directives** at controlled points in the schedule
(`backend/gauntlet/engine/controller.py`). No fault is simulated by declaring a test failed. Every
actor runs in its own OS process behind statement gates, so the engine can:

* pause an actor *between any two SQL statements*;
* hard-kill the process (`TerminateProcess` on Windows, `SIGKILL` elsewhere);
* drop a response the actor has already produced.

After a kill, the harness waits until PostgreSQL has terminated the orphaned backend before
continuing, so whatever transaction was open has really been rolled back.

The injection points are defined in terms of statement classes (`before_write`, `before_commit`,
`after_commit`), not in terms of a particular implementation's code. The same schedule therefore
applies unchanged to any implementation.

| Id | Fault | Scenarios |
|---|---|---|
| F1 | Crash after commit, before the client receives confirmation | S03, S04 |
| F2 | Retry of an operation whose outcome is uncertain | S03 |
| F3 | Legacy instance writes during backfill | S02 |
| F4 | Backfill batch interrupted and retried | S04 |
| F5 | Migration step executed more than once | S04 |
| F6 | New version encounters a record that has not been backfilled | S06 |
| F7 | Process restarted during a migration stage | S04 |
| F8 | Schema constraint introduced while old instances depend on the old schema | S05 |

---

## F1: Crash after commit, before confirmation

* **Preconditions.** A write operation is in flight on an app or migration actor.
* **Injection point.** One of two variants:
  * (a) `lose_response`: the actor reaches its response gate after committing, and the harness
    discards the response.
  * (b) `run_until(after_commit, 1)` followed by `crash`: the actor runs until its first COMMIT
    (or autocommit write) completes, then its process is hard-killed. For an implementation that
    writes in two transactions, this lands *between* them.
* **Expected behaviour.** The client records the attempt as `uncertain`. Any committed effect
  persists exactly once.
* **Expected invariant outcomes.** S7 holds; S3 holds even when a process died part-way through a
  logical operation.
* **Recovery obligation.** A retry with the same idempotency key returns the original result
  without applying the operation again.
* **Trace.** `invoke` → `stmt`… → `respond(delivered=false)` or `crash` → `restart` →
  `invoke(retry=true)` → … → `respond`.

## F2: Retry of an uncertain operation

* **Preconditions.** An earlier attempt of the same logical operation ended `uncertain` (F1).
* **Injection point.** The client invokes the logical operation again with the same idempotency
  key, on the same or a different instance.
* **Expected behaviour.** The operation is applied at most once per key, and the retry is
  acknowledged with the original result.
* **Expected invariant outcomes.** S4 and S7 hold.
* **Recovery obligation.** None beyond correct idempotency.

## F3: Legacy instance writes during backfill

* **Preconditions.** The schema is expanded and a backfill batch is in flight.
* **Injection point.**
  * The scripted form, `run_until(before_write)`: the backfill actor is paused just before its
    first write statement (after its reads). v1 instances then commit writes to rows in that
    batch, and the batch resumes.
  * The randomised form, `interleave`: a seeded random policy chooses which actor's next
    statement to grant at each step.
* **Expected behaviour.** The backfill never writes a value computed from data that changed
  before the write.
* **Expected invariant outcomes.** S1, S3 and S7 hold.
* **Recovery obligation.** None.

## F4: Backfill batch interrupted and retried

* **Preconditions.** A backfill batch is in flight in the migrator process.
* **Injection point.** The migrator is hard-killed either just before its COMMIT, so the batch
  must roll back, or just after it, so the response is lost.
* **Expected behaviour.** No partial batch is visible, and the backfill resumes from persisted
  progress.
* **Expected invariant outcomes.** R3 holds; S3, S5 and S7 hold.
* **Recovery obligation.** Restart the worker and continue backfilling until it completes, within
  a bounded number of batches.

## F5: Migration step executed more than once

* **Preconditions.** A migration step (`expand`, `backfill_batch`, `finalize` or `contract`) has
  already succeeded.
* **Injection point.** `repeat` re-invokes the same logical step between two raw snapshots.
* **Expected behaviour.** The step is a no-op, or refuses explicitly.
* **Expected invariant outcomes.** R2 holds.
* **Recovery obligation.** None.

## F6: New version on a not-yet-backfilled record

* **Preconditions.** The schema is expanded, v2 is serving, and the backfill has not run.
* **Injection point.** v2 reads, charges and overwrites rows that exist only in the legacy
  column. The amounts are chosen (by `Script.amount`) so that binary floating point cannot
  represent them exactly. This is followed by a seeded random v1/v2 interleaving.
* **Expected behaviour.** v2 derives values from the legacy column and stays exact to the cent.
* **Expected invariant outcomes.** S2 and S7 hold.
* **Recovery obligation.** None.

## F7: Process restarted during a migration stage

* **Preconditions.** `expand` or `finalize` is executing inside a transaction.
* **Injection point.** The migrator process is hard-killed just before its COMMIT.
* **Expected behaviour.** PostgreSQL rolls the step back (DDL is transactional), and no partial
  schema change survives.
* **Expected invariant outcomes.** R1 holds, and R4 holds after the retry.
* **Recovery obligation.** Restart the worker and retry the step, at most 3 attempts.

## F8: Constraint introduced while old instances still depend on the old schema

* **Preconditions.** The backfill is complete, and v1 instances are still registered and
  serving.
* **Injection point.** `finalize` (which sets NOT NULL on `total_cents`) and `contract` (which
  drops `total`) are requested before v1 drains, and v1 keeps writing.
* **Expected behaviour.** Both steps refuse explicitly until v1 has drained, and v1 requests keep
  succeeding.
* **Expected invariant outcomes.** C1 holds. The refusals are expected failures, not violations.
  R4 holds after the drain.
* **Recovery obligation.** Drain v1, then retry `finalize` and `contract`.

---

## Reproducing a fault trace

Every run records its full directive list (`schedule`) and its statement-level event trace. To
re-execute a run:

```
python -m gauntlet run --scenario S04_crash_and_resume --impl ref_a_trigger --seed 1
python -m gauntlet replay <run_id>     # re-executes a persisted run and compares hashes
```
