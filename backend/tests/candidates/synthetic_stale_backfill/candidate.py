"""SYNTHETIC TEST FIXTURE -- NOT produced by an AI coding agent.

The adapted copy of reference A with one injected defect: the backfill computes cents from
values read before its writes (the m1_stale_backfill defect). Expected to fail S1/S7 in S02.
It exists only to exercise GAUNTLET's external-candidate loader, isolation and evaluation workflow.
"""


from __future__ import annotations

from gauntlet.impls.base import AppError, Db, ImplementationBase, claim_key, store_response
from gauntlet.money import MoneyError, decimal_to_cents, format_cents, parse_cents

STAGES = ("legacy", "expanded", "backfilling", "backfilled", "finalized", "contracted")

SYNC_FUNCTION_SQL = """
CREATE OR REPLACE FUNCTION gauntlet_sync_total() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  IF TG_OP = 'INSERT' THEN
    IF NEW.total_cents IS NULL AND NEW.total IS NOT NULL THEN
      NEW.total_cents := (NEW.total * 100)::bigint;
    ELSIF NEW.total IS NULL AND NEW.total_cents IS NOT NULL THEN
      NEW.total := NEW.total_cents::numeric / 100;
    ELSIF NEW.total IS NOT NULL AND NEW.total_cents IS NOT NULL
          AND NEW.total_cents <> (NEW.total * 100)::bigint THEN
      RAISE EXCEPTION 'total and total_cents disagree on insert';
    END IF;
  ELSE
    IF NEW.total IS DISTINCT FROM OLD.total AND NEW.total_cents IS NOT DISTINCT FROM OLD.total_cents THEN
      NEW.total_cents := (NEW.total * 100)::bigint;
    ELSIF NEW.total_cents IS DISTINCT FROM OLD.total_cents AND NEW.total IS NOT DISTINCT FROM OLD.total THEN
      NEW.total := NEW.total_cents::numeric / 100;
    ELSIF NEW.total_cents IS DISTINCT FROM OLD.total_cents AND NEW.total IS DISTINCT FROM OLD.total
          AND NEW.total_cents <> (NEW.total * 100)::bigint THEN
      RAISE EXCEPTION 'total and total_cents disagree on update';
    END IF;
  END IF;
  RETURN NEW;
END
$$
"""


class MigrationCommon:
    """Helpers shared by both reference strategies."""

    def _cents(self, text: str) -> int:
        try:
            return parse_cents(text)
        except MoneyError as exc:
            raise AppError("invalid_amount", str(exc)) from exc

    def _stage_shared(self, db: Db) -> str:
        # FOR KEY SHARE blocks finalize/contract (FOR UPDATE) for the duration of
        # this request's transaction, but not the backfill cursor update.
        row = db.one("SELECT stage FROM migration_state WHERE id = 1 FOR KEY SHARE")
        if row is None:
            raise AppError("no_migration_state")
        return row["stage"]

    def _stage_exclusive(self, db: Db) -> dict:
        row = db.one("SELECT stage, backfill_cursor FROM migration_state WHERE id = 1 FOR UPDATE")
        if row is None:
            raise AppError("no_migration_state")
        return row

    def assert_legacy_drained(self, db: Db) -> None:
        row = db.one("SELECT count(*) AS n FROM app_instances WHERE version = 'v1'")
        if row["n"]:
            raise AppError("migration_refused", f"{row['n']} legacy (v1) instance(s) still registered")

    def _require_expanded(self, stage: str) -> None:
        if stage == "legacy":
            raise AppError("not_expanded", "v2 cannot serve before the schema is expanded")

    def migrator_backfill_batch(self, db: Db, a: dict) -> dict:
        size = int(a.get("batch_size", 100))
        if size <= 0:
            raise AppError("invalid_argument", "batch_size must be positive")
        with db.transaction():
            st = db.one("SELECT stage, backfill_cursor FROM migration_state WHERE id = 1 FOR NO KEY UPDATE")
            if st["stage"] == "legacy":
                raise AppError("not_expanded")
            if st["stage"] in ("backfilled", "finalized", "contracted"):
                return {"updated": 0, "done": True, "cursor": st["backfill_cursor"]}
            rows = db.query("SELECT id FROM orders WHERE id > %s ORDER BY id LIMIT %s",
                            (st["backfill_cursor"], size))
            ids = [r["id"] for r in rows]
            updated = self._backfill_rows(db, ids) if ids else 0
            done = len(ids) < size
            cursor = self._next_cursor(ids, st["backfill_cursor"])
            db.execute("UPDATE migration_state SET backfill_cursor = %s, stage = %s WHERE id = 1",
                       (cursor, "backfilled" if done else "backfilling"))
            return {"updated": updated, "done": done, "cursor": cursor}

    def _next_cursor(self, ids: list[int], old: int) -> int:
        return ids[-1] if ids else old


class Implementation(MigrationCommon):
    strategy = "A"

    # -- v2 application ------------------------------------------------------
    def v2_get_order(self, db: Db, a: dict) -> dict:
        with db.transaction():
            stage = self._stage_shared(db)
            self._require_expanded(stage)
            if stage in ("finalized", "contracted"):
                row = db.one("SELECT total_cents FROM orders WHERE id = %s", (a["order_id"],))
            else:
                # Not-yet-backfilled rows: derive from the legacy column.
                row = db.one("SELECT COALESCE(total_cents, (total * 100)::bigint) AS total_cents "
                             "FROM orders WHERE id = %s", (a["order_id"],))
        if row is None:
            raise AppError("not_found", f"order {a['order_id']}")
        return {"total": format_cents(int(row["total_cents"]))}

    def v2_create_order(self, db: Db, a: dict) -> dict:
        cents = self._cents(a["total"])
        with db.transaction():
            self._require_expanded(self._stage_shared(db))
            prior = claim_key(db, a["key"])
            if prior is not None:
                return prior
            # The trigger (until contract) fills the legacy column.
            db.execute("INSERT INTO orders (id, total_cents) VALUES (%s, %s)", (a["order_id"], cents))
            resp = {"order_id": a["order_id"], "total": format_cents(cents)}
            store_response(db, a["key"], resp)
            return resp

    def v2_set_total(self, db: Db, a: dict) -> dict:
        cents = self._cents(a["total"])
        with db.transaction():
            self._require_expanded(self._stage_shared(db))
            prior = claim_key(db, a["key"])
            if prior is not None:
                return prior
            n = db.execute("UPDATE orders SET total_cents = %s WHERE id = %s", (cents, a["order_id"]))
            if n == 0:
                raise AppError("not_found", f"order {a['order_id']}")
            resp = {"total": format_cents(cents)}
            store_response(db, a["key"], resp)
            return resp

    def v2_add_charge(self, db: Db, a: dict) -> dict:
        delta = self._cents(a["amount"])
        with db.transaction():
            stage = self._stage_shared(db)
            self._require_expanded(stage)
            prior = claim_key(db, a["key"])
            if prior is not None:
                return prior
            row = db.one(self._add_charge_sql(stage), (delta, a["order_id"]))
            if row is None:
                raise AppError("not_found", f"order {a['order_id']}")
            resp = {"total": format_cents(int(row["total_cents"]))}
            store_response(db, a["key"], resp)
            return resp

    def _add_charge_sql(self, stage: str) -> str:
        if stage == "contracted":
            return "UPDATE orders SET total_cents = total_cents + %s WHERE id = %s RETURNING total_cents"
        # A row may not be backfilled yet: start from the legacy value.
        return ("UPDATE orders SET total_cents = COALESCE(total_cents, (total * 100)::bigint) + %s "
                "WHERE id = %s RETURNING total_cents")

    # -- migration -----------------------------------------------------------
    def migrator_expand(self, db: Db, a: dict) -> dict:
        with db.transaction():
            st = self._stage_exclusive(db)
            if st["stage"] != "legacy":
                return {"noop": True, "stage": st["stage"]}
            db.execute("ALTER TABLE orders ADD COLUMN IF NOT EXISTS total_cents BIGINT")
            db.execute(SYNC_FUNCTION_SQL)
            db.execute("DROP TRIGGER IF EXISTS orders_sync_total ON orders")
            db.execute("CREATE TRIGGER orders_sync_total BEFORE INSERT OR UPDATE ON orders "
                       "FOR EACH ROW EXECUTE FUNCTION gauntlet_sync_total()")
            db.execute("UPDATE migration_state SET stage = 'expanded' WHERE id = 1")
            return {"noop": False, "stage": "expanded"}

    def _backfill_rows(self, db: Db, ids: list[int]) -> int:
        # Value computed from the row's current version inside the statement;
        # rows already synced by the trigger are skipped.
        return db.execute("UPDATE orders SET total_cents = (total * 100)::bigint "
                          "WHERE id = ANY(%s) AND total_cents IS NULL", (ids,))

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
            bad = db.one("SELECT count(*) AS n FROM orders WHERE total_cents <> (total * 100)::bigint")["n"]
            if bad:
                raise AppError("migration_refused", f"{bad} row(s) where representations disagree")
            db.execute("ALTER TABLE orders ALTER COLUMN total_cents SET NOT NULL")
            db.execute("UPDATE migration_state SET stage = 'finalized' WHERE id = 1")
            return {"noop": False, "stage": "finalized"}

    def migrator_contract(self, db: Db, a: dict) -> dict:
        with db.transaction():
            st = self._stage_exclusive(db)
            if st["stage"] == "contracted":
                return {"noop": True, "stage": "contracted"}
            if st["stage"] != "finalized":
                raise AppError("migration_refused", f"not finalized (stage={st['stage']})")
            self.assert_legacy_drained(db)
            db.execute("DROP TRIGGER IF EXISTS orders_sync_total ON orders")
            db.execute("DROP FUNCTION IF EXISTS gauntlet_sync_total()")
            db.execute("ALTER TABLE orders DROP COLUMN IF EXISTS total")
            db.execute("UPDATE migration_state SET stage = 'contracted' WHERE id = 1")
            return {"noop": False, "stage": "contracted"}

    # Injected defect (copied from mutant m1_stale_backfill): values are read before they are written.
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
