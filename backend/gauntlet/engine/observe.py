"""Raw database observation by the harness (used by the oracle).

These queries run on the harness's own monitor connection and read the
physical state directly. They never call implementation code and never rely
on an implementation's own notion of what the totals should be.
"""

from __future__ import annotations

import psycopg


def capture(conn: psycopg.Connection) -> dict:
    cols = [r[0] for r in conn.execute(
        "SELECT column_name FROM information_schema.columns "
        "WHERE table_schema = 'public' AND table_name = 'orders' ORDER BY ordinal_position").fetchall()]
    not_null = {r[0]: r[1] == "NO" for r in conn.execute(
        "SELECT column_name, is_nullable FROM information_schema.columns "
        "WHERE table_schema = 'public' AND table_name = 'orders'").fetchall()}
    triggers = [r[0] for r in conn.execute(
        "SELECT tgname FROM pg_trigger WHERE tgrelid = 'public.orders'::regclass AND NOT tgisinternal "
        "ORDER BY tgname").fetchall()]
    select = ["id"]
    if "total" in cols:
        select.append("total::text AS total")
    if "total_cents" in cols:
        select.append("total_cents")
    rows = []
    for r in conn.execute(f"SELECT {', '.join(select)} FROM orders ORDER BY id").fetchall():
        row = {"id": int(r[0])}
        k = 1
        if "total" in cols:
            row["total"] = r[k]
            k += 1
        if "total_cents" in cols:
            row["total_cents"] = None if r[k] is None else int(r[k])
        rows.append(row)
    try:
        stage = conn.execute("SELECT stage FROM migration_state WHERE id = 1").fetchone()
        stage = stage[0] if stage else None
    except psycopg.Error:
        stage = None
    return {"columns": cols, "not_null": not_null, "triggers": triggers, "rows": rows,
            "impl_reported_stage": stage}


def blocking_pids(conn: psycopg.Connection, pid: int) -> list[int]:
    row = conn.execute("SELECT pg_blocking_pids(%s)", (pid,)).fetchone()
    return list(row[0]) if row and row[0] else []


def backend_alive(conn: psycopg.Connection, pid: int) -> bool:
    return conn.execute("SELECT 1 FROM pg_stat_activity WHERE pid = %s", (pid,)).fetchone() is not None
