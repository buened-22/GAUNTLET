"""Contract between the harness and an implementation under evaluation.

An implementation is a class exposing methods named ``<role>_<op>`` where role
is ``v1`` (legacy app), ``v2`` (new app) or ``migrator``. Each method receives
a :class:`Db` handle and a dict of arguments and returns a JSON-serialisable
result, or raises :class:`AppError` for an explicit, client-visible failure.

Implementations run inside an actor subprocess. Every database statement goes
through ``Db``, which lets the harness pause, interleave, or crash the actor at
statement boundaries without knowing anything about the implementation.

Client operations (both app versions)::

    create_order {order_id, total, key}      -> {"order_id", "total"}
    set_total    {order_id, total, key}      -> {"total"}
    add_charge   {order_id, amount, key}     -> {"total"}   (total after charge)
    get_order    {order_id}                  -> {"total"}   or AppError("not_found")

Migration operations (migrator role)::

    expand {} ; backfill_batch {batch_size} -> {"updated", "done"} ; finalize {} ; contract {}

All monetary values are decimal strings with exactly two decimals.
"""

from __future__ import annotations

import json
from contextlib import AbstractContextManager
from typing import Any, Protocol


class AppError(Exception):
    """An explicit failure reported to the client (the operation did not commit)."""

    def __init__(self, code: str, message: str = ""):
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message


class Db(Protocol):
    def query(self, sql: str, params: Any = None) -> list[dict]: ...
    def one(self, sql: str, params: Any = None) -> dict | None: ...
    def execute(self, sql: str, params: Any = None) -> int: ...
    def transaction(self) -> AbstractContextManager[None]: ...


class ImplementationBase:
    """Protocol-level operations shared by all implementations (not mutated)."""

    strategy: str = "?"

    # Instance lifecycle: called by the harness when an app actor starts/drains.
    def app_register(self, db: Db, args: dict) -> dict:
        db.execute(
            "INSERT INTO app_instances (instance_id, version) VALUES (%s, %s) "
            "ON CONFLICT (instance_id) DO UPDATE SET version = EXCLUDED.version",
            (args["instance_id"], args["version"]),
        )
        return {"registered": args["instance_id"]}

    def app_deregister(self, db: Db, args: dict) -> dict:
        db.execute("DELETE FROM app_instances WHERE instance_id = %s", (args["instance_id"],))
        return {"deregistered": args["instance_id"]}


# --- Idempotency helper used by the reference implementations ---------------

def claim_key(db: Db, key: str) -> dict | None:
    """Claim an idempotency key inside the caller's transaction.

    Returns ``None`` if the key is new (caller must do the work and then call
    :func:`store_response`), otherwise the stored response of the earlier
    attempt. A concurrent attempt with the same key blocks on the unique index
    until the first one commits or aborts, so a key is applied at most once.
    """
    inserted = db.execute(
        "INSERT INTO processed_requests (key, response) VALUES (%s, 'null'::jsonb) "
        "ON CONFLICT (key) DO NOTHING",
        (key,),
    )
    if inserted == 1:
        return None
    row = db.one("SELECT response FROM processed_requests WHERE key = %s", (key,))
    return row["response"] if row else None


def store_response(db: Db, key: str, response: dict) -> None:
    db.execute(
        "UPDATE processed_requests SET response = %s::jsonb WHERE key = %s",
        (json.dumps(response), key),
    )


def current_stage(db: Db) -> str:
    row = db.one("SELECT stage FROM migration_state WHERE id = 1")
    if row is None:
        raise AppError("no_migration_state", "migration_state row missing")
    return row["stage"]
