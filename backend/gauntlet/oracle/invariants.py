"""Invariant catalogue (metadata). Verification logic lives in checker.py."""

from __future__ import annotations

from dataclasses import asdict, dataclass


class Status:
    PASS = "pass"
    FAIL = "fail"
    INCONCLUSIVE = "inconclusive"
    NOT_APPLICABLE = "not_applicable"
    INFRA_ERROR = "infra_error"


@dataclass(frozen=True)
class InvariantDef:
    id: str
    category: str  # "safety" | "recovery" | "compatibility"
    title: str
    statement: str
    procedure: str

    def to_dict(self) -> dict:
        return asdict(self)


INVARIANTS: list[InvariantDef] = [
    InvariantDef(
        "S7_LINEARIZABLE", "safety", "Client-visible history is linearizable",
        "For every order, there is a total order of the operations consistent with real time in which "
        "every acknowledged operation takes effect exactly once, every failed operation takes no effect, "
        "every uncertain operation takes effect at most once (within its execution window), and every "
        "read result and harness snapshot equals the model state at its position.",
        "Per-order Wing-Gong style search over the ledger and harness snapshots, against an exact-cents "
        "register/counter model. Relaxed models (below) are used only to diagnose a failure.",
    ),
    InvariantDef(
        "S1_ACKED_DURABLE", "safety", "Acknowledged writes are not silently lost",
        "No acknowledged write disappears from the authoritative state.",
        "PASS if S7 passes. If S7 fails, FAIL when the history becomes linearizable after allowing "
        "acknowledged writes to be dropped; otherwise INCONCLUSIVE.",
    ),
    InvariantDef(
        "S2_EXACT_CENTS", "safety", "Monetary values are exact to the cent",
        "Every client-visible amount parses as an exact two-decimal value and no value deviates from the "
        "exact model by rounding.",
        "Syntactic check of every response amount; plus, if S7 fails, FAIL when allowing each written "
        "amount to be off by one cent makes the history linearizable.",
    ),
    InvariantDef(
        "S3_REPR_AGREEMENT", "safety", "Old and new representations agree where both are authoritative",
        "At every harness snapshot where the strategy's contract requires both columns to be authoritative, "
        "total * 100 = total_cents for every row (and total_cents is non-NULL where required).",
        "Direct check of raw snapshots taken by the harness's own connection.",
    ),
    InvariantDef(
        "S4_NO_DUPLICATION", "safety", "Retries do not duplicate logical operations",
        "A logical operation (identified by its idempotency key) takes effect at most once.",
        "PASS if S7 passes. If S7 fails, FAIL when allowing each write to take effect twice makes the "
        "history linearizable; otherwise INCONCLUSIVE.",
    ),
    InvariantDef(
        "S5_RECORDS_PRESERVED", "safety", "Records do not disappear or become corrupted",
        "Every order whose creation was acknowledged (or seeded) exists in every later snapshot with a "
        "non-NULL authoritative value; no row appears that no operation could have created.",
        "Direct check of raw snapshots against the ledger.",
    ),
    InvariantDef(
        "S6_FAILED_NOT_APPLIED", "safety", "Failed operations are not reported as committed",
        "An operation reported to the client as failed takes no effect.",
        "PASS if S7 passes. If S7 fails, FAIL when allowing failed writes to take effect makes the "
        "history linearizable; otherwise INCONCLUSIVE.",
    ),
    InvariantDef(
        "R1_INTERRUPTED_RESUMES", "recovery", "An interrupted migration step can resume safely",
        "After a migration step is interrupted by a process crash, re-running it (at most 3 attempts, on a "
        "restarted worker, with no further faults) succeeds.",
        "Ledger check over migration operations with an uncertain attempt.",
    ),
    InvariantDef(
        "R2_REPEAT_SAFE", "recovery", "Repeating a completed migration step does not corrupt state",
        "Re-executing a migration step that already succeeded either succeeds or refuses with an explicit "
        "application-level error, and leaves the orders table unchanged: the same columns, NOT NULL flags, "
        "triggers and every stored row value (in every column, authoritative or not). Implementation-owned "
        "migration control state (migration_state) is not part of the comparison.",
        "Harness snapshots of the orders table taken immediately before and after each repeated step are "
        "compared; a changed snapshot, or a failure that is not an explicit application-level refusal "
        "(e.g. a database error), is a FAIL; an uncertain outcome is INCONCLUSIVE.",
    ),
    InvariantDef(
        "R3_BACKFILL_RETRY_SAFE", "recovery", "Backfill can be retried safely",
        "After an interrupted or repeated backfill batch, the backfill completes within "
        "ceil(N / batch_size) + 3 further batches, and S3/S7 hold afterwards.",
        "Ledger check of backfill attempts plus the S3/S7 results.",
    ),
    InvariantDef(
        "R4_TERMINAL_STATE", "recovery", "The migration reaches a valid terminal state",
        "Assumptions: after the last injected fault, crashed workers are restarted, V1 instances are "
        "drained, and each step is retried at most 3 times. Under these assumptions the migration reaches "
        "the contracted state: column total absent, total_cents NOT NULL, no sync trigger left.",
        "Ledger (contract acknowledged) plus schema introspection of the final database.",
    ),
    InvariantDef(
        "C1_COMPATIBILITY", "compatibility", "Live old versions are not broken by schema changes",
        "No request served by a registered application instance fails because of a schema "
        "incompatibility (SQLSTATE 42703 undefined_column, 42P01 undefined_table, 23502 not_null_violation).",
        "Ledger check of failed operations from app actors registered at invocation time.",
    ),
]

INVARIANT_IDS = [i.id for i in INVARIANTS]
BY_ID = {i.id: i for i in INVARIANTS}
