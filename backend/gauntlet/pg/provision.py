"""Per-run database provisioning and the harness-owned legacy (P0) schema.

The P0 schema is owned by the harness, not by implementations, so every
implementation starts from an identical, recorded initial state.
"""

from __future__ import annotations

import secrets
import uuid
from dataclasses import dataclass

import psycopg
from psycopg import conninfo

from gauntlet.config import PG_ADMIN_DSN

LEGACY_SCHEMA_SQL = """
CREATE TABLE orders (
    id    BIGINT PRIMARY KEY,
    total NUMERIC(12,2) NOT NULL
);
-- Idempotency records used by the legacy application for client request keys.
CREATE TABLE processed_requests (
    key      TEXT PRIMARY KEY,
    response JSONB NOT NULL
);
-- Registry of live application instances (each instance registers before
-- serving and deregisters when drained). The migration protocol uses it to
-- decide whether legacy instances have drained.
CREATE TABLE app_instances (
    instance_id TEXT PRIMARY KEY,
    version     TEXT NOT NULL CHECK (version IN ('v1', 'v2'))
);
-- Migration control row, available to implementations.
CREATE TABLE migration_state (
    id              INT PRIMARY KEY CHECK (id = 1),
    stage           TEXT NOT NULL,
    backfill_cursor BIGINT NOT NULL DEFAULT 0
);
INSERT INTO migration_state (id, stage) VALUES (1, 'legacy');
"""


# Server-side bound on the harness's own DDL (create / drop / initialise a run database), so cleanup and setup
# cannot block indefinitely; generous because CREATE DATABASE copies a template and may be slow under load.
DDL_STATEMENT_TIMEOUT_MS = 120_000


class InfrastructureError(RuntimeError):
    """A failure of the harness or its environment (not of the system under test)."""


@dataclass
class RunDatabase:
    name: str
    dsn: str


def check_server(admin_dsn: str = PG_ADMIN_DSN) -> dict:
    """Return server facts, raising InfrastructureError if unreachable."""
    try:
        with psycopg.connect(admin_dsn, autocommit=True, connect_timeout=5) as conn:
            version = conn.execute("SHOW server_version").fetchone()[0]
            num = int(conn.execute("SHOW server_version_num").fetchone()[0])
    except psycopg.Error as exc:
        raise InfrastructureError(f"PostgreSQL unreachable at configured DSN: {exc}") from exc
    return {"server_version": version, "server_version_num": num, "is_pg16": 160000 <= num < 170000}


def create_run_database(admin_dsn: str = PG_ADMIN_DSN, prefix: str = "gauntlet_run") -> RunDatabase:
    name = f"{prefix}_{uuid.uuid4().hex[:12]}"
    try:
        with psycopg.connect(admin_dsn, autocommit=True, connect_timeout=5) as conn:
            conn.execute(f"SET statement_timeout = {DDL_STATEMENT_TIMEOUT_MS}")
            conn.execute(f'CREATE DATABASE "{name}" TEMPLATE template0 ENCODING UTF8')
            # Other roles (e.g. a concurrently running candidate's role) must not connect to this run's
            # database; the harness connects as the cluster owner and is unaffected.
            conn.execute(f'REVOKE CONNECT, TEMPORARY ON DATABASE "{name}" FROM PUBLIC')
    except psycopg.Error as exc:
        raise InfrastructureError(f"could not create run database: {exc}") from exc
    params = conninfo.conninfo_to_dict(admin_dsn)
    params["dbname"] = name
    return RunDatabase(name=name, dsn=conninfo.make_conninfo(**params))


def drop_run_database(db: RunDatabase, admin_dsn: str = PG_ADMIN_DSN) -> None:
    try:
        with psycopg.connect(admin_dsn, autocommit=True, connect_timeout=5) as conn:
            conn.execute(f"SET statement_timeout = {DDL_STATEMENT_TIMEOUT_MS}")
            conn.execute(f'DROP DATABASE IF EXISTS "{db.name}" WITH (FORCE)')
    except psycopg.Error:
        pass  # best effort; leftover databases are harmless and can be removed later


def create_candidate_role(db: RunDatabase, admin_dsn: str = PG_ADMIN_DSN) -> tuple[str, RunDatabase]:
    """A per-run login role for an external candidate: not a superuser, no CREATEDB / CREATEROLE /
    REPLICATION / BYPASSRLS, owner of this run database only (so it can perform the migration DDL), and
    CONNECT on the run database revoked from PUBLIC. Returns (role name, connection info as that role)."""
    from psycopg import sql
    role = "gauntlet_cand_" + db.name.rsplit("_", 1)[-1]
    password = secrets.token_hex(24)
    try:
        with psycopg.connect(admin_dsn, autocommit=True, connect_timeout=5) as conn:
            conn.execute(f"SET statement_timeout = {DDL_STATEMENT_TIMEOUT_MS}")
            conn.execute(sql.SQL("CREATE ROLE {} LOGIN PASSWORD {} NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION "
                                 "NOBYPASSRLS CONNECTION LIMIT 16").format(sql.Identifier(role), sql.Literal(password)))
            conn.execute(sql.SQL("ALTER DATABASE {} OWNER TO {}").format(sql.Identifier(db.name), sql.Identifier(role)))
            conn.execute(sql.SQL("REVOKE ALL ON DATABASE {} FROM PUBLIC").format(sql.Identifier(db.name)))
            conn.execute(sql.SQL("GRANT CONNECT, TEMPORARY ON DATABASE {} TO {}").format(sql.Identifier(db.name),
                                                                                        sql.Identifier(role)))
    except psycopg.Error as exc:
        raise InfrastructureError(f"could not create the candidate database role: {exc}") from exc
    params = conninfo.conninfo_to_dict(admin_dsn)
    params.update(dbname=db.name, user=role, password=password)
    return role, RunDatabase(name=db.name, dsn=conninfo.make_conninfo(**params))


def create_observer_role(db: RunDatabase, admin_dsn: str = PG_ADMIN_DSN) -> tuple[str, RunDatabase]:
    """A per-run login role for the harness's snapshot reads of an external candidate's run database (audit
    finding A-02): not a superuser, no CREATEDB / CREATEROLE / REPLICATION / BYPASSRLS, member of no role, and
    CONNECT on this run database only (no TEMPORARY). Its read privileges on the tables are granted by the
    candidate role itself (:func:`grant_observer_read`), so no superuser session opens in the run database.
    Returns (role name, connection info as that role)."""
    from psycopg import sql
    role = "gauntlet_obs_" + db.name.rsplit("_", 1)[-1]
    password = secrets.token_hex(24)
    try:
        with psycopg.connect(admin_dsn, autocommit=True, connect_timeout=5) as conn:
            conn.execute(f"SET statement_timeout = {DDL_STATEMENT_TIMEOUT_MS}")
            conn.execute(sql.SQL("CREATE ROLE {} LOGIN PASSWORD {} NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION "
                                 "NOBYPASSRLS NOINHERIT CONNECTION LIMIT 2").format(sql.Identifier(role),
                                                                                    sql.Literal(password)))
            conn.execute(sql.SQL("GRANT CONNECT ON DATABASE {} TO {}").format(sql.Identifier(db.name),
                                                                              sql.Identifier(role)))
    except psycopg.Error as exc:
        raise InfrastructureError(f"could not create the observer database role: {exc}") from exc
    params = conninfo.conninfo_to_dict(admin_dsn)
    params.update(dbname=db.name, user=role, password=password)
    return role, RunDatabase(name=db.name, dsn=conninfo.make_conninfo(**params))


def grant_observer_read(candidate_dsn: str, observer_role: str) -> None:
    """As the candidate role (owner of the run database, its ``public`` schema and the P0 tables), before any
    candidate code runs: let the observer read the schema and its tables, including tables created later."""
    from psycopg import sql
    obs = sql.Identifier(observer_role)
    try:
        with psycopg.connect(candidate_dsn, autocommit=True, connect_timeout=5) as conn:
            conn.execute(f"SET statement_timeout = {DDL_STATEMENT_TIMEOUT_MS}")
            conn.execute(sql.SQL("GRANT USAGE ON SCHEMA public TO {}").format(obs))
            conn.execute(sql.SQL("GRANT SELECT ON ALL TABLES IN SCHEMA public TO {}").format(obs))
            conn.execute(sql.SQL("ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT SELECT ON TABLES TO {}").format(obs))
    except psycopg.Error as exc:
        raise InfrastructureError(f"could not grant the observer read access: {exc}") from exc


def drop_candidate_role(role: str, admin_dsn: str = PG_ADMIN_DSN) -> bool:
    """Best effort (after the run database is dropped). Returns False if the role could not be removed.
    Also used for the per-run observer role."""
    from psycopg import sql
    try:
        with psycopg.connect(admin_dsn, autocommit=True, connect_timeout=5) as conn:
            conn.execute(f"SET statement_timeout = {DDL_STATEMENT_TIMEOUT_MS}")
            conn.execute(sql.SQL("DROP ROLE IF EXISTS {}").format(sql.Identifier(role)))
        return True
    except psycopg.Error:
        return False


def apply_legacy_schema(dsn: str, initial_orders: dict[int, str]) -> None:
    """Create the P0 schema and seed the initial orders (id -> decimal string)."""
    try:
        with psycopg.connect(dsn, autocommit=False, connect_timeout=5) as conn:
            conn.execute(f"SET statement_timeout = {DDL_STATEMENT_TIMEOUT_MS}")
            conn.execute(LEGACY_SCHEMA_SQL)
            with conn.cursor() as cur:
                for oid, total in sorted(initial_orders.items()):
                    cur.execute("INSERT INTO orders (id, total) VALUES (%s, %s::numeric)", (oid, total))
            conn.commit()
    except psycopg.Error as exc:
        raise InfrastructureError(f"could not initialise legacy schema: {exc}") from exc
