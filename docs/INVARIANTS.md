# Invariants

This document defines every invariant the oracle checks, and the exact procedure it uses. The
implementation is in `backend/gauntlet/oracle/` (`checker.py`, `linearizability.py`). The oracle
imports **no implementation code**. Its inputs are:

1. **The ledger.** This is the append-only record the harness keeps while acting as the client
   (`oracle/ledger.py`). For every attempt it records the operation, the arguments, the actor, whether the
   actor was a registered app instance, the invocation sequence number, and the outcome:
   * `ok`: the client received a success response.
   * `failed`: the client received an explicit error.
   * `uncertain`: no response. The process was killed, the response was dropped, or the run ended
     with the operation still in flight.
2. **Raw snapshots.** The harness reads these on its own monitor connection (`engine/observe.py`):
   column list, NOT NULL flags, triggers and every row of `orders`. The
   `migration_state.stage` value written by the implementation is recorded only for display and
   is never trusted.
3. **The initial state** from the schedule definition.
4. **The strategy's declared protocol contract**, which states when the two columns must agree.
   It is part of the strategy's specification (see below), not something read from the database.

All arithmetic is exact. Amounts are parsed from decimal strings into integer cents, and binary
floating point is never used.

## Logical time

Every engine step emits an event with a strictly increasing sequence number `seq`. An attempt
invoked at `seq = i` and completed (response delivered, or crash / response loss observed) at
`seq = c` may take effect only inside the open interval `(i, c)`. The checker doubles every
sequence number, so a snapshot taken at `seq = q` occupies `(2q-1, 2q+1)`. Wall-clock times are
recorded but play no part in any check.

## Authoritative column

When the oracle reads a snapshot it uses the column that is authoritative *according to the
ledger*:

| Condition at snapshot `q` | Authoritative value |
|---|---|
| `total` column absent (contracted) | `total_cents` |
| `total_cents` column absent | `total` |
| a `finalize` attempt was acknowledged before `q` | `total_cents` |
| a `finalize` attempt is in flight or uncertain at `q` | either (both values are admissible) |
| otherwise | `total` |

A NULL in the authoritative column is recorded as `CORRUPT`, which matches no model state.

---

## S7_LINEARIZABLE: client-visible history is linearizable (core check)

**Statement.** For each order there is a total order of operations, consistent with real time,
such that:

* every logical write with an acknowledged attempt takes effect exactly once;
* every logical write whose attempts were all `uncertain` takes effect at most once;
* no `failed` attempt takes effect;
* every acknowledged read, and every harness snapshot, equals the model state at its position.

**Model (per order).** The state is either `ABSENT` or an integer number of cents.

| Operation | Precondition | Effect | Output check |
|---|---|---|---|
| `create(v)` | `ABSENT` | state := v | – |
| `set(v)` | present | state := v | – |
| `add(d)` | present | state := state + d | an acknowledged result must equal the new state |
| `read` | – | – | observed value ∈ admissible outputs |

All attempts that share an idempotency key form one **logical write**, even when the client gave
them different internal operation ids. Its single effect may be placed inside the window of any
`ok` or `uncertain` attempt. Writes with different keys are different operations, even when their
payloads are identical. A write without a key falls back to its internal operation id.

If one key is reused for a *different* request (another operation, order or amount), the oracle
cannot tell which submission the server honoured.

* **Excluded from checking.** The histories of the affected orders are left out of the
  linearizability check. Each one is recorded in S7's evidence as inconclusive ("not checked"),
  together with the conflicting attempts. They are never reported as PASS.
* **S7 can still FAIL.** If no other order has a violation, S7 is **INCONCLUSIVE**. If another order
  has a confirmed violation, S7 is **FAIL**, and the excluded orders are listed as not checked.
* **Violations can be hidden.** An order with conflicting key reuse cannot be evaluated reliably, so a
  genuine violation on that same order is not detected. It appears only as inconclusive.

**Procedure.** The checker runs a depth-first search over linearization sequences, memoised on
`(time cursor, model value, items done)`. Items are placed greedily at the earliest feasible point
inside one of their windows, which is optimal for a fixed sequence. The search has a node budget
of 300,000 per order. If the budget runs out, the result is **INCONCLUSIVE**, never PASS.

**Evidence.** For each failing order the checker records the initial value, every logical write
with its attempts and outputs, the *first conflicting observation*, and the relaxations that
explain the failure. The first conflicting observation is the earliest read or snapshot at which
the truncated history stops being linearizable, and its `seq` drives the UI's "triggering event"
marker.

## Diagnoses by model relaxation (S1, S2, S4, S6)

If S7 fails for an order, the oracle re-runs the search under four relaxed models. Each one makes
the history linearizable only if the violation is *consistent with* a specific class of bug:

| Invariant | Relaxation | Meaning |
|---|---|---|
| **S1_ACKED_DURABLE** | `lost_ack`: the state may revert to any value the order held earlier (≤ 3 reverts) | an acknowledged write was lost, either never applied or overwritten by stale data |
| **S4_NO_DUPLICATION** | `duplicate`: every write may take effect a second time | a retry was applied twice |
| **S6_FAILED_NOT_APPLIED** | `failed_applied`: failed attempts may take effect | an operation reported as failed actually committed |
| **S2_EXACT_CENTS** | `rounding`: each written amount may be off by ±1 cent | rounding or truncation |

Under a relaxation, outputs of acknowledged writes are not checked; reads and snapshots still
are.

Status rules for each of these invariants:

* **PASS** if S7 passes.
* **FAIL** if, for at least one failing order, its relaxation makes the history linearizable.
* **INCONCLUSIVE** otherwise. If S7 failed and this relaxation does not explain it, the invariant
  can be neither confirmed nor refuted.

**S2** also has a syntactic part. Every amount in a response must be a canonical decimal string
with exactly two decimal places, or S2 fails.

> These are diagnoses, not proofs of root cause. Wording in reports and the UI says "consistent with".

## S3_REPR_AGREEMENT: representations agree where both are authoritative

**Statement.** At every snapshot where the strategy's contract requires agreement,
`total * 100 = total_cents` for every row in which both are non-NULL. Where the contract also
requires `total_cents` to be non-NULL, it must be.

**Contract by strategy** (based on acknowledged migration steps in the ledger; snapshots taken
while a step is uncertain are skipped):

| Strategy | Agreement required | `total_cents` NOT NULL required |
|---|---|---|
| A (trigger) | after `expand` is acknowledged, until `contract` | after `finalize` is acknowledged |
| B (dual-write) | after `finalize` is acknowledged, until `contract` | after `finalize` is acknowledged |

**NOT_APPLICABLE** if no snapshot falls in a window where agreement is required.

## S5_RECORDS_PRESERVED: records do not disappear or become corrupted

For every snapshot:

* every seeded order, and every order whose `create` was acknowledged before the snapshot, is
  present;
* no row exists whose id no `create` attempt (of any status) used;
* no row has a NULL authoritative value.

## R1_INTERRUPTED_RESUMES: interrupted steps resume

**Assumption.** After a crash, the worker is restarted and the step is retried at most 3 times
with no further faults.

**Check.** For each logical migration step with an attempt that ended `uncertain/crashed`, a
later attempt must be acknowledged within 3 attempts. If the step was never retried the result is
INCONCLUSIVE. **NOT_APPLICABLE** if no migration step crashed.

## R2_REPEAT_SAFE: repeating a completed step is harmless

The `repeat` directive takes a snapshot, re-invokes an already-completed step, and takes another
snapshot. **PASS** requires two things:

* the attempt is `ok`, or `failed` with an explicit application-level refusal;
* the two snapshots are identical: columns, NOT NULL flags, triggers and every row.

An uncertain outcome is INCONCLUSIVE.

## R3_BACKFILL_RETRY_SAFE: backfill retry safety

**Applicable** when a backfill batch was interrupted (uncertain) or repeated. **PASS** requires
all of the following:

* (a) a later batch reports `done` within `ceil(N / batch_size) + 3` batches after the first
  interruption, where N is the maximum row count seen in any snapshot;
* (b) `finalize` is subsequently acknowledged;
* (c) S3 and S5 did not fail.

Value correctness is left to S7.

## R4_TERMINAL_STATE: the migration reaches a valid terminal state

**Assumptions.** After the last injected fault, crashed workers are restarted, v1 instances are
drained, and each step is retried at most 3 times. These assumptions are encoded in every
scenario's directives.

**Check.** A `contract` attempt was acknowledged, and introspection of the final snapshot shows:

* no `total` column;
* `total_cents` is NOT NULL;
* no triggers remain on `orders`;
* no NULL `total_cents` values.

If the run had an incomplete or stuck condition, a failure is reported as INCONCLUSIVE.

This is a **bounded** liveness check. It says nothing about schedules outside the scenario's
stated assumptions.

## C1_COMPATIBILITY: live old versions are not broken

A request from an app actor that was registered in `app_instances` when it was invoked must not
fail with any of these SQLSTATEs:

* `42703` undefined_column
* `42P01` undefined_table
* `23502` not_null_violation

Explicit refusals by migration steps (for example, finalize refusing while v1 is registered) are
expected failures, not violations.

---

## Run verdict

The verdict is the first rule that applies:

1. `infra_error` if any infrastructure error occurred. In that case every invariant is reported
   as `infra_error` and nothing is evaluated.
2. `fail` if any invariant is FAIL.
3. `inconclusive` if any invariant is INCONCLUSIVE, the engine recorded an incomplete condition,
   or no invariant actually PASSED.
4. `pass` otherwise.

`not_applicable` never counts as a pass. Tests in `backend/tests/test_ledger_oracle.py` cover these
rules, including an oracle that detects a wrong final state the implementation reported as
successful, and runs that are inconclusive or have infrastructure errors never being reported as
passing.
