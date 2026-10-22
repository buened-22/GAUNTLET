"""Version 1: the legacy application. It knows only ``orders.total``.

V1 is identical for every strategy: legacy code cannot be changed by the
migration, which is exactly what makes the coexistence window hard.
"""

from __future__ import annotations

from gauntlet.impls.base import AppError, Db, claim_key, store_response
from gauntlet.money import MoneyError, decimal_to_cents, format_cents, parse_cents


def _amount(text: str) -> str:
    try:
        return format_cents(parse_cents(text))
    except MoneyError as exc:
        raise AppError("invalid_amount", str(exc)) from exc


class LegacyV1:
    def v1_create_order(self, db: Db, a: dict) -> dict:
        total = _amount(a["total"])
        with db.transaction():
            prior = claim_key(db, a["key"])
            if prior is not None:
                return prior
            db.execute("INSERT INTO orders (id, total) VALUES (%s, %s::numeric)", (a["order_id"], total))
            resp = {"order_id": a["order_id"], "total": total}
            store_response(db, a["key"], resp)
            return resp

    def v1_set_total(self, db: Db, a: dict) -> dict:
        total = _amount(a["total"])
        with db.transaction():
            prior = claim_key(db, a["key"])
            if prior is not None:
                return prior
            n = db.execute("UPDATE orders SET total = %s::numeric WHERE id = %s", (total, a["order_id"]))
            if n == 0:
                raise AppError("not_found", f"order {a['order_id']}")
            resp = {"total": total}
            store_response(db, a["key"], resp)
            return resp

    def v1_add_charge(self, db: Db, a: dict) -> dict:
        amount = _amount(a["amount"])
        with db.transaction():
            prior = claim_key(db, a["key"])
            if prior is not None:
                return prior
            row = db.one("UPDATE orders SET total = total + %s::numeric WHERE id = %s RETURNING total",
                         (amount, a["order_id"]))
            if row is None:
                raise AppError("not_found", f"order {a['order_id']}")
            resp = {"total": format_cents(decimal_to_cents(row["total"]))}
            store_response(db, a["key"], resp)
            return resp

    def v1_get_order(self, db: Db, a: dict) -> dict:
        row = db.one("SELECT total FROM orders WHERE id = %s", (a["order_id"],))
        if row is None:
            raise AppError("not_found", f"order {a['order_id']}")
        return {"total": format_cents(decimal_to_cents(row["total"]))}
