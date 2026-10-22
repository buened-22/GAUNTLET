"""Fault catalogue (metadata). Injection is implemented by engine directives."""

from __future__ import annotations

FAULTS = [
    {
        "id": "F1", "title": "Crash after commit, before the client receives confirmation",
        "preconditions": "A write operation is in flight on an application or migration actor.",
        "injection_point": "Either (a) the actor reaches its response gate after committing and the harness "
                           "discards the response (lose_response), or (b) the harness lets the actor run until "
                           "its first COMMIT (or autocommit write) completes and then hard-kills the process "
                           "(run_until after_commit + crash).",
        "expected_behavior": "The client records the attempt as uncertain. The committed effect persists exactly once.",
        "expected_invariants": "S7 holds (the effect may or may not be present until a retry resolves it); "
                               "S3 holds even if the process died between the writes of a logical operation.",
        "recovery_obligation": "A retry with the same idempotency key returns the original result without "
                               "re-applying the operation.",
        "directives": ["lose_response", "run_until(after_commit)", "crash"],
        "scenarios": ["S03_lost_ack_and_retry", "S04_crash_and_resume"],
    },
    {
        "id": "F2", "title": "Retry of an operation whose outcome is uncertain",
        "preconditions": "An earlier attempt of the same logical operation ended uncertain (F1).",
        "injection_point": "The client re-invokes the logical operation with the same idempotency key, on the "
                           "same or a different instance.",
        "expected_behavior": "At-most-once application per key; the retry is acknowledged with the original result.",
        "expected_invariants": "S4 and S7 hold.",
        "recovery_obligation": "None beyond correct idempotency.",
        "directives": ["call(retry=True)"],
        "scenarios": ["S03_lost_ack_and_retry"],
    },
    {
        "id": "F3", "title": "Legacy instance writes during backfill",
        "preconditions": "Schema expanded; a backfill batch is in flight.",
        "injection_point": "The backfill actor is paused immediately before its first write statement (after "
                           "its reads); v1 instances then commit writes to rows of that batch; the batch resumes. "
                           "Also exercised by seeded random statement-level interleavings.",
        "expected_behavior": "The backfill never writes a value computed from data that changed before its write.",
        "expected_invariants": "S1, S3, S7 hold.",
        "recovery_obligation": "None.",
        "directives": ["run_until(before_write)", "interleave"],
        "scenarios": ["S02_backfill_vs_legacy_writes"],
    },
    {
        "id": "F4", "title": "Backfill batch interrupted and retried",
        "preconditions": "A backfill batch is in flight in a separate OS process.",
        "injection_point": "The migrator process is hard-killed either just before its COMMIT (batch must "
                           "roll back) or just after it (response lost).",
        "expected_behavior": "No partial batch is visible; the backfill resumes from persisted progress.",
        "expected_invariants": "R3 holds; S3/S5/S7 hold.",
        "recovery_obligation": "Restart the worker and continue backfilling until completion (bounded).",
        "directives": ["run_until(before_commit|after_commit)", "crash", "restart", "retry_until_ok"],
        "scenarios": ["S04_crash_and_resume"],
    },
    {
        "id": "F5", "title": "Migration step executed more than once",
        "preconditions": "A migration step (expand, backfill_batch, finalize, contract) has already succeeded.",
        "injection_point": "The same logical step is invoked again; the harness snapshots the raw state "
                           "immediately before and after.",
        "expected_behavior": "The step is a no-op (or refuses explicitly) and changes nothing.",
        "expected_invariants": "R2 holds.",
        "recovery_obligation": "None.",
        "directives": ["repeat"],
        "scenarios": ["S04_crash_and_resume"],
    },
    {
        "id": "F6", "title": "New version encounters a record that has not been backfilled",
        "preconditions": "Schema expanded, v2 serving, backfill not yet run.",
        "injection_point": "v2 reads, charges and overwrites legacy-only rows; amounts chosen so that binary "
                           "floating point cannot represent them; plus a seeded random v1/v2 interleaving.",
        "expected_behavior": "v2 derives values from the legacy column and stays exact to the cent.",
        "expected_invariants": "S2 and S7 hold.",
        "recovery_obligation": "None.",
        "directives": ["call/run", "interleave"],
        "scenarios": ["S06_unbackfilled_records"],
    },
    {
        "id": "F7", "title": "Process restarted during a migration stage",
        "preconditions": "expand or finalize is executing inside a transaction.",
        "injection_point": "The migrator process is hard-killed immediately before its COMMIT.",
        "expected_behavior": "PostgreSQL rolls the step back; no partial schema change survives.",
        "expected_invariants": "R1 holds; R4 holds after the retry.",
        "recovery_obligation": "Restart the worker and retry the step (at most 3 attempts).",
        "directives": ["run_until(before_commit)", "crash", "restart", "retry_until_ok"],
        "scenarios": ["S04_crash_and_resume"],
    },
    {
        "id": "F8", "title": "Schema constraint introduced while old instances depend on the old schema",
        "preconditions": "Backfill complete; v1 instances still registered and serving.",
        "injection_point": "finalize (NOT NULL on total_cents) and contract (drop total) are requested before "
                           "the v1 instances drain; v1 keeps writing.",
        "expected_behavior": "Both steps refuse explicitly until v1 has drained; v1 requests keep succeeding.",
        "expected_invariants": "C1 holds (refusals are expected failures, not violations); R4 holds after the drain.",
        "recovery_obligation": "Drain v1, then retry finalize and contract.",
        "directives": ["call/run", "drain", "retry_until_ok"],
        "scenarios": ["S05_premature_finalization"],
    },
]

BY_ID = {f["id"]: f for f in FAULTS}
