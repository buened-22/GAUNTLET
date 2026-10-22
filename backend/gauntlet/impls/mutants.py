"""Deliberately faulty implementations (mutants).

Each mutant overrides exactly one method of a reference implementation and
models a realistic engineering mistake. None of them introduces syntax
errors, crashes, or setup failures; each must be killed by its *intended*
invariant (validated by ``gauntlet bench``). Metadata lives in registry.py.
"""

from __future__ import annotations

from gauntlet.impls.base import AppError, Db, claim_key, store_response
from gauntlet.impls.reference_a import ReferenceA
from gauntlet.impls.reference_b import ReferenceB
from gauntlet.money import decimal_to_cents, format_cents


class StaleBackfillA(ReferenceA):
    """M1: backfill reads values into memory, then writes them in later statements.

    A legacy write that commits between the read and the write is overwritten
    with the stale value (the trigger then copies it back into ``total``).
    """

    def migrator_backfill_batch(self, db: Db, a: dict) -> dict:
        size = int(a.get("batch_size", 100))
        with db.transaction():
            st = db.one("SELECT stage, backfill_cursor FROM migration_state WHERE id = 1 FOR NO KEY UPDATE")
            if st["stage"] == "legacy":
                raise AppError("not_expanded")
            if st["stage"] in ("backfilled", "finalized", "contracted"):
                return {"updated": 0, "done": True, "cursor": st["backfill_cursor"]}
            rows = db.query("SELECT id, total FROM orders WHERE id > %s ORDER BY id LIMIT %s",
                            (st["backfill_cursor"], size))
            for r in rows:  # BUG: value computed from a snapshot taken before these writes
                db.execute("UPDATE orders SET total_cents = %s WHERE id = %s",
                           (decimal_to_cents(r["total"]), r["id"]))
            done = len(rows) < size
            cursor = rows[-1]["id"] if rows else st["backfill_cursor"]
            db.execute("UPDATE migration_state SET backfill_cursor = %s, stage = %s WHERE id = 1",
                       (cursor, "backfilled" if done else "backfilling"))
            return {"updated": len(rows), "done": done, "cursor": cursor}


class NonAtomicDualWriteB(ReferenceB):
    """M2: V2 add_charge writes the two representations in two transactions.

    Transaction 1 updates ``total`` and records the idempotency key;
    transaction 2 updates ``total_cents``. A crash between them leaves the
    columns disagreeing, and an idempotent retry then acknowledges the
    charge without ever completing the second write.
    """

    def v2_add_charge(self, db: Db, a: dict) -> dict:
        delta = self._cents(a["amount"])
        with db.transaction():
            stage = self._stage_shared(db)
            self._require_expanded(stage)
            prior = claim_key(db, a["key"])
            if prior is not None:
                return prior
            if stage == "contracted":
                row = db.one("UPDATE orders SET total_cents = total_cents + %s WHERE id = %s RETURNING total_cents",
                             (delta, a["order_id"]))
                if row is None:
                    raise AppError("not_found", f"order {a['order_id']}")
                resp = {"total": format_cents(int(row["total_cents"]))}
                store_response(db, a["key"], resp)
                return resp
            row = db.one("UPDATE orders SET total = total + %s::numeric / 100 WHERE id = %s RETURNING total",
                         (delta, a["order_id"]))
            if row is None:
                raise AppError("not_found", f"order {a['order_id']}")
            new_cents = decimal_to_cents(row["total"])
            resp = {"total": format_cents(new_cents)}
            store_response(db, a["key"], resp)
        # BUG: second representation written in a separate transaction.
        with db.transaction():
            db.execute("UPDATE orders SET total_cents = %s WHERE id = %s", (new_cents, a["order_id"]))
        return resp


class RetryDuplicatesA(ReferenceA):
    """M3: V2 add_charge records the idempotency key but never checks it.

    A retry of a charge whose first attempt committed (but whose response
    was lost) is applied a second time.
    """

    def v2_add_charge(self, db: Db, a: dict) -> dict:
        delta = self._cents(a["amount"])
        with db.transaction():
            stage = self._stage_shared(db)
            self._require_expanded(stage)
            row = db.one(self._add_charge_sql(stage), (delta, a["order_id"]))
            if row is None:
                raise AppError("not_found", f"order {a['order_id']}")
            resp = {"total": format_cents(int(row["total_cents"]))}
            # BUG: upsert instead of claim-then-check.
            db.execute("INSERT INTO processed_requests (key, response) VALUES (%s, %s::jsonb) "
                       "ON CONFLICT (key) DO UPDATE SET response = EXCLUDED.response",
                       (a["key"], f'{{"total": "{resp["total"]}"}}'))
            return resp


class PrematureContractA(ReferenceA):
    """M4: the drain check is missing, so finalize/contract proceed while
    legacy instances are still registered and serving. Contract then drops
    the column those instances depend on."""

    def assert_legacy_drained(self, db: Db) -> None:
        return None  # BUG: no drain check


class BackfillSkipsRowA(ReferenceA):
    """M5: off-by-one cursor; the row immediately after each batch is skipped."""

    def _next_cursor(self, ids: list[int], old: int) -> int:
        return ids[-1] + 1 if ids else old  # BUG: query uses id > cursor


class FloatCentsA(ReferenceA):
    """M6: V2 converts decimal strings to cents through binary floating point."""

    def _cents(self, text: str) -> int:
        try:
            return int(float(text) * 100)  # BUG: e.g. 19.99 -> 1998
        except ValueError as exc:
            raise AppError("invalid_amount", str(exc)) from exc


class AssumesBackfilledA(ReferenceA):
    """M7: V2 add_charge assumes every row is already backfilled and treats a
    missing ``total_cents`` as zero."""

    def _add_charge_sql(self, stage: str) -> str:
        return ("UPDATE orders SET total_cents = COALESCE(total_cents, 0) + %s "  # BUG
                "WHERE id = %s RETURNING total_cents")


class SkipReconcileB(ReferenceB):
    """M8: finalize assumes dual-writes kept both columns in sync, so it skips
    reconciliation and only checks for NULLs before switching authority.
    Legacy writes made after the backfill passed a row are lost."""

    def migrator_finalize(self, db: Db, a: dict) -> dict:
        with db.transaction():
            st = self._stage_exclusive(db)
            if st["stage"] in ("finalized", "contracted"):
                return {"noop": True, "stage": st["stage"]}
            if st["stage"] != "backfilled":
                raise AppError("migration_refused", f"backfill not complete (stage={st['stage']})")
            self.assert_legacy_drained(db)
            nulls = db.one("SELECT count(*) AS n FROM orders WHERE total_cents IS NULL")["n"]
            if nulls:
                raise AppError("migration_refused", f"{nulls} row(s) with NULL total_cents")
            # BUG: no reconciliation, no agreement check.
            db.execute("ALTER TABLE orders ALTER COLUMN total_cents SET NOT NULL")
            db.execute("UPDATE migration_state SET stage = 'finalized' WHERE id = 1")
            return {"noop": False, "stage": "finalized", "reconciled": 0}
