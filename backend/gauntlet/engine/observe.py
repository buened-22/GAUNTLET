"""Raw database observation by the harness (used by the oracle).

These queries run on the harness's own monitor connection and read the
physical state directly. They never call implementation code and never rely
on an implementation's own notion of what the totals should be.

Every name is schema-qualified, so a candidate cannot redirect a query through ``search_path`` (audit finding
A-02), and the observed tables must be plain tables: a view, foreign table or partitioned table in their place
could run candidate-defined code inside the harness's session or report rows that are not stored.
"""

from __future__ import annotations

import psycopg

from gauntlet.pg.provision import InfrastructureError


class ObservationObstructed(InfrastructureError):
    """The harness could not observe the run database as it requires: an observed table was replaced by
    something other than a plain table, its read access was revoked, or row-level security would filter it.
    For an external candidate this is a candidate execution failure (the candidate controls these objects);
    it is never a pass."""


_RELKIND = ("SELECT c.relkind FROM pg_catalog.pg_class c JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace "
            "WHERE n.nspname = 'public' AND c.relname = %s")


def _relkind(conn: psycopg.Connection, table: str) -> str | None:
    row = conn.execute(_RELKIND, (table,)).fetchone()
    return row[0] if row else None


def capture(conn: psycopg.Connection) -> dict:
    cols = [r[0] for r in conn.execute(
        "SELECT column_name FROM information_schema.columns "
        "WHERE table_schema = 'public' AND table_name = 'orders' ORDER BY ordinal_position").fetchall()]
    not_null = {r[0]: r[1] == "NO" for r in conn.execute(
        "SELECT column_name, is_nullable FROM information_schema.columns "
        "WHERE table_schema = 'public' AND table_name = 'orders'").fetchall()}
    triggers = [r[0] for r in conn.execute(
        "SELECT tgname FROM pg_catalog.pg_trigger WHERE tgrelid = 'public.orders'::regclass AND NOT tgisinternal "
        "ORDER BY tgname").fetchall()]
    kind = _relkind(conn, "orders")
    if kind != "r":
        raise ObservationObstructed(f"public.orders is not a plain table (relkind {kind!r}); it was not read")
    select = ["id"]
    if "total" in cols:
        select.append("total::text AS total")
    if "total_cents" in cols:
        select.append("total_cents")
    rows = []
    for r in conn.execute(f"SELECT {', '.join(select)} FROM public.orders ORDER BY id").fetchall():
        row = {"id": int(r[0])}
        k = 1
        if "total" in cols:
            row["total"] = r[k]
            k += 1
        if "total_cents" in cols:
            row["total_cents"] = None if r[k] is None else int(r[k])
        rows.append(row)
    try:
        if _relkind(conn, "migration_state") not in (None, "r"):
            stage = None  # not a plain table: not read (its content is only the implementation's own report)
        else:
            stage = conn.execute("SELECT stage FROM public.migration_state WHERE id = 1").fetchone()
            stage = stage[0] if stage else None
    except psycopg.Error:
        stage = None
    return {"columns": cols, "not_null": not_null, "triggers": triggers, "rows": rows,
            "impl_reported_stage": stage}


def blocking_pids(conn: psycopg.Connection, pid: int) -> list[int]:
    row = conn.execute("SELECT pg_catalog.pg_blocking_pids(%s)", (pid,)).fetchone()
    return list(row[0]) if row and row[0] else []


def backend_activity(conn: psycopg.Connection, pid: int, timeout_ms: int = 5000) -> dict | None:
    """The server's view of one backend, for classifying a step timeout: state, wait event and how long its
    current statement has been running. Bounded by a transaction-local statement_timeout, so the monitor
    session's own settings are unchanged. None if the backend no longer exists."""
    with conn.transaction():
        conn.execute(f"SET LOCAL statement_timeout = {int(timeout_ms)}")
        row = conn.execute(
            "SELECT state, wait_event_type, wait_event, "
            "EXTRACT(EPOCH FROM pg_catalog.clock_timestamp() - query_start)::float8 "
            "FROM pg_catalog.pg_stat_activity WHERE pid = %s", (pid,)).fetchone()
    if row is None:
        return None
    return {"state": row[0], "wait_event_type": row[1], "wait_event": row[2],
            "elapsed_s": None if row[3] is None else round(float(row[3]), 3)}


def backend_alive(conn: psycopg.Connection, pid: int) -> bool:
    return conn.execute("SELECT 1 FROM pg_catalog.pg_stat_activity WHERE pid = %s", (pid,)).fetchone() is not None
