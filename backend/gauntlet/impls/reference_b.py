"""Reference Strategy B: atomic application dual-write + post-drain reconciliation.

Protocol
--------
expand    Add ``total_cents BIGINT`` (nullable). No trigger.
coexist   ``total`` stays authoritative while V1 instances may be alive
          (V1 writes only ``total``, so ``total_cents`` can go stale). V2 writes
          BOTH columns in a single UPDATE/INSERT statement inside one
          transaction, and reads ``total``.
backfill  Bounded, cursor-based batches that copy ``total`` into
          ``total_cents`` (an optimisation that reduces reconciliation work).
finalize  Requires: backfill complete and no registered V1 instance. Then, in
          one transaction: reconcile every row (``total_cents := total*100``
          where they differ), verify, set NOT NULL, switch authority to
          ``total_cents``. V2 keeps dual-writing atomically so ``total`` stays
          a faithful mirror until contract (rollback safety).
contract  Requires finalize and no registered V1 instance; drops ``total``.

Correctness argument: before finalize, ``total`` is authoritative and every
writer updates it; the reconciliation runs only after V1 has drained, so no
legacy write can occur after it; after finalize, every writer (V2 only)
updates both columns atomically.
"""

from __future__ import annotations

from gauntlet.impls.base import AppError, Db, ImplementationBase, claim_key, store_response
from gauntlet.impls.legacy import LegacyV1
from gauntlet.impls.reference_a import MigrationCommon
from gauntlet.money import decimal_to_cents, format_cents


class ReferenceB(MigrationCommon, LegacyV1, ImplementationBase):
    strategy = "B"

    # -- v2 application ------------------------------------------------------
    def v2_get_order(self, db: Db, a: dict) -> dict:
        with db.transaction():
            stage = self._stage_shared(db)
            self._require_expanded(stage)
            if stage in ("finalized", "contracted"):
                row = db.one("SELECT total_cents FROM orders WHERE id = %s", (a["order_id"],))
                cents = None if row is None else int(row["total_cents"])
            else:
                row = db.one("SELECT total FROM orders WHERE id = %s", (a["order_id"],))
                cents = None if row is None else decimal_to_cents(row["total"])
        if cents is None:
            raise AppError("not_found", f"order {a['order_id']}")
        return {"total": format_cents(cents)}

    def v2_create_order(self, db: Db, a: dict) -> dict:
        cents = self._cents(a["total"])
        with db.transaction():
            stage = self._stage_shared(db)
            self._require_expanded(stage)
            prior = claim_key(db, a["key"])
            if prior is not None:
                return prior
            if stage == "contracted":
                db.execute("INSERT INTO orders (id, total_cents) VALUES (%s, %s)", (a["order_id"], cents))
            else:
                db.execute("INSERT INTO orders (id, total, total_cents) VALUES (%s, %s::numeric / 100, %s)",
                           (a["order_id"], cents, cents))
            resp = {"order_id": a["order_id"], "total": format_cents(cents)}
            store_response(db, a["key"], resp)
            return resp

    def v2_set_total(self, db: Db, a: dict) -> dict:
        cents = self._cents(a["total"])
        with db.transaction():
            stage = self._stage_shared(db)
            self._require_expanded(stage)
            prior = claim_key(db, a["key"])
            if prior is not None:
                return prior
            n = self._write_set(db, stage, a["order_id"], cents)
            if n == 0:
                raise AppError("not_found", f"order {a['order_id']}")
            resp = {"total": format_cents(cents)}
            store_response(db, a["key"], resp)
            return resp

    def _write_set(self, db: Db, stage: str, order_id: int, cents: int) -> int:
        if stage == "contracted":
            return db.execute("UPDATE orders SET total_cents = %s WHERE id = %s", (cents, order_id))
        return db.execute("UPDATE orders SET total = %s::numeric / 100, total_cents = %s WHERE id = %s",
                          (cents, cents, order_id))

    def v2_add_charge(self, db: Db, a: dict) -> dict:
        delta = self._cents(a["amount"])
        with db.transaction():
            stage = self._stage_shared(db)
            self._require_expanded(stage)
            prior = claim_key(db, a["key"])
            if prior is not None:
                return prior
            new_cents = self._write_add(db, stage, a["order_id"], delta)
            if new_cents is None:
                raise AppError("not_found", f"order {a['order_id']}")
            resp = {"total": format_cents(new_cents)}
            store_response(db, a["key"], resp)
            return resp

    def _write_add(self, db: Db, stage: str, order_id: int, delta: int) -> int | None:
        if stage == "contracted":
            row = db.one("UPDATE orders SET total_cents = total_cents + %s WHERE id = %s RETURNING total_cents",
                         (delta, order_id))
        elif stage == "finalized":
            # total_cents authoritative; mirror into total in the same statement.
            row = db.one("UPDATE orders SET total_cents = total_cents + %(d)s, "
                         "total = (total_cents + %(d)s)::numeric / 100 "
                         "WHERE id = %(id)s RETURNING total_cents", {"d": delta, "id": order_id})
        else:
            # total authoritative; derive total_cents from it in the same statement.
            row = db.one("UPDATE orders SET total = total + %(d)s::numeric / 100, "
                         "total_cents = ((total + %(d)s::numeric / 100) * 100)::bigint "
                         "WHERE id = %(id)s RETURNING total_cents", {"d": delta, "id": order_id})
        return None if row is None else int(row["total_cents"])

    # -- migration -----------------------------------------------------------
    def migrator_expand(self, db: Db, a: dict) -> dict:
        with db.transaction():
            st = self._stage_exclusive(db)
            if st["stage"] != "legacy":
                return {"noop": True, "stage": st["stage"]}
            db.execute("ALTER TABLE orders ADD COLUMN IF NOT EXISTS total_cents BIGINT")
            db.execute("UPDATE migration_state SET stage = 'expanded' WHERE id = 1")
            return {"noop": False, "stage": "expanded"}

    def _backfill_rows(self, db: Db, ids: list[int]) -> int:
        return db.execute("UPDATE orders SET total_cents = (total * 100)::bigint "
                          "WHERE id = ANY(%s) AND total_cents IS DISTINCT FROM (total * 100)::bigint", (ids,))

    def migrator_finalize(self, db: Db, a: dict) -> dict:
        with db.transaction():
            st = self._stage_exclusive(db)
            if st["stage"] in ("finalized", "contracted"):
                return {"noop": True, "stage": st["stage"]}
            if st["stage"] != "backfilled":
                raise AppError("migration_refused", f"backfill not complete (stage={st['stage']})")
            self.assert_legacy_drained(db)
            reconciled = self._reconcile(db)
            bad = db.one("SELECT count(*) AS n FROM orders WHERE total_cents IS DISTINCT FROM (total * 100)::bigint")["n"]
            if bad:
                raise AppError("migration_refused", f"{bad} row(s) disagree after reconciliation")
            db.execute("ALTER TABLE orders ALTER COLUMN total_cents SET NOT NULL")
            db.execute("UPDATE migration_state SET stage = 'finalized' WHERE id = 1")
            return {"noop": False, "stage": "finalized", "reconciled": reconciled}

    def _reconcile(self, db: Db) -> int:
        return db.execute("UPDATE orders SET total_cents = (total * 100)::bigint "
                          "WHERE total_cents IS DISTINCT FROM (total * 100)::bigint")

    def migrator_contract(self, db: Db, a: dict) -> dict:
        with db.transaction():
            st = self._stage_exclusive(db)
            if st["stage"] == "contracted":
                return {"noop": True, "stage": "contracted"}
            if st["stage"] != "finalized":
                raise AppError("migration_refused", f"not finalized (stage={st['stage']})")
            self.assert_legacy_drained(db)
            db.execute("ALTER TABLE orders DROP COLUMN IF EXISTS total")
            db.execute("UPDATE migration_state SET stage = 'contracted' WHERE id = 1")
            return {"noop": False, "stage": "contracted"}
