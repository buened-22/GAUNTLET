"""Full-audit hardening: PostgreSQL-backed regressions. Candidates here are SYNTHETIC, trusted fixtures
written for these tests (never AI-generated code); process isolation is not a security boundary."""

from __future__ import annotations

import os
import shutil
import pathlib

import psycopg
import pytest

from gauntlet.config import PG_ADMIN_DSN

pytestmark = pytest.mark.pg

HERE = pathlib.Path(__file__).resolve().parent
GOOD = HERE / "candidates" / "synthetic_ref_a_copy"
TRUSTED = {"mode": "process", "trusted_environment": True}


def _run_dbs() -> list[str]:
    with psycopg.connect(PG_ADMIN_DSN, autocommit=True) as c:
        return [r[0] for r in c.execute("SELECT datname FROM pg_database WHERE datname LIKE 'gauntlet_run_%'")]


@pytest.fixture
def cand_env(tmp_path, monkeypatch, pg_available):
    monkeypatch.setenv("GAUNTLET_CANDIDATES_DIR", str(tmp_path / "snapshots"))
    return tmp_path


def _register(src: pathlib.Path) -> str:
    from gauntlet.candidates.snapshots import register
    return "ext:" + register(src)["candidate_id"]


def test_cleanup_continues_and_slot_is_released_when_pool_close_fails(cand_env, monkeypatch):
    """Before the fix a failing pool.close() skipped dropping the run database and releasing the slot."""
    import gauntlet.engine.runner as runner
    from gauntlet.actor.proxy import ProcessPool
    from gauntlet.candidates import sandbox

    class FailingPool(ProcessPool):
        def close(self):
            super().close()
            raise OSError("simulated pool cleanup failure")

    monkeypatch.setattr(runner, "ProcessPool", FailingPool)
    impl = _register(GOOD)
    before = set(_run_dbs())
    for _ in range(3):  # more runs than execution slots: a leaked slot would block or fail the third run
        run = runner.execute_run("S01_baseline_migration", impl, 1, isolation=TRUSTED, time_limit_s=180)
        assert run["verdict"] == "pass", run["infra_error"]
        assert any("simulated pool cleanup failure" in e for e in run["cleanup_errors"])
    assert set(_run_dbs()) == before
    slots = sandbox._slots
    assert slots is not None and slots._value == int(os.environ.get("GAUNTLET_MAX_CANDIDATE_RUNS",
                                                                     sandbox.DEFAULTS["max_concurrent_runs"]))


def _bypassing_candidate(dst: pathlib.Path) -> pathlib.Path:
    """A synthetic candidate that tries to execute ONE statement on a raw connection (`db._conn`) instead
    of the gated handle: the A-01 bypass of v0.4.0."""
    shutil.copytree(GOOD, dst)
    src = (dst / "candidate.py").read_text(encoding="utf-8")
    gated = 'db.execute("INSERT INTO orders (id, total_cents) VALUES (%s, %s)", (a["order_id"], cents))'
    assert src.count(gated) == 1
    src = src.replace(gated, 'db._conn.execute("INSERT INTO orders (id, total_cents) VALUES (%s, %s)", '
                             '(a["order_id"], cents))')
    (dst / "candidate.py").write_text(src, encoding="utf-8")
    return dst


def test_former_bypass_attempt_fails_and_nothing_reaches_the_database(cand_env):
    """A-01 (resolved): the actor process has no connection, so `db._conn` does not exist. The statement the
    candidate tried to run outside the gate never reaches the database."""
    from gauntlet.candidates.contract import validate
    from gauntlet.engine.runner import execute_run
    d = _bypassing_candidate(cand_env / "bypass")
    assert validate(d).ok  # static validation still does not catch it; it no longer needs to
    run = execute_run("S01_baseline_migration", _register(d), 1, isolation=TRUSTED, time_limit_s=180)
    assert run["infra_error"] is None, run["infra_error"]
    v2_creates = [a for a in run["attempts"] if a["op"] == "create_order" and a["actor"].startswith("v2")]
    assert v2_creates and all(a["status"] == "failed" and "_conn" in a["error"]["message"] for a in v2_creates)
    final_ids = {r["id"] for r in run["snapshots"][-1]["rows"]}
    assert not any(int(a["args"]["order_id"]) in final_ids for a in v2_creates)


PROBE = '''
def _probe(db):
    found_types = set()
    for o in gc.get_objects():
        mod = getattr(type(o), "__module__", None)
        if isinstance(mod, str) and mod.split(".")[0] in ("psycopg", "psycopg_binary", "psycopg_c"):
            found_types.add(mod + "." + type(o).__name__)
    found_types = sorted(found_types)
    try:
        builtins.__import__("psycopg")
        imp = "allowed"
    except PermissionError:
        imp = "denied"
    except ImportError:
        imp = "unavailable"
    return {"driver_modules": sorted(m for m in sys.modules if m.split(".")[0] in ("psycopg", "psycopg_binary", "psycopg_c")),
            "driver_objects": found_types, "db_attributes": sorted(vars(db)), "import_driver": imp,
            "dsn_in_environment": any("postgres" in v.lower() for v in os.environ.values())}


class Probe(Implementation):
    def migrator_expand(self, db, a):
        return _probe(db)
'''


def test_candidate_process_holds_no_database_connection_or_driver(cand_env):
    """From inside a real sandboxed candidate actor: no database driver is loaded, no connection object
    exists anywhere on the heap, the db handle is a request channel only, and the environment holds no DSN."""
    import json
    from gauntlet.engine.runner import execute_run
    d = cand_env / "probe"
    shutil.copytree(GOOD, d)
    src = (d / "candidate.py").read_text(encoding="utf-8").replace(
        "from __future__ import annotations\n", "from __future__ import annotations\n\nimport builtins, gc, os, sys\n", 1)
    (d / "candidate.py").write_text(src + PROBE, encoding="utf-8")
    m = json.loads((d / "gauntlet_candidate.json").read_text(encoding="utf-8"))
    m["name"], m["entry"]["class"] = "probe", "Probe"
    (d / "gauntlet_candidate.json").write_text(json.dumps(m), encoding="utf-8")
    run = execute_run("S01_baseline_migration", _register(d), 1, isolation=TRUSTED, time_limit_s=180)
    probe = next(a for a in run["attempts"] if a["op"] == "expand")
    assert probe["status"] == "ok", probe
    info = probe["result"]
    assert info["driver_modules"] == [] and info["driver_objects"] == []
    assert info["db_attributes"] == ["_chan", "_in_txn_flag", "_seq", "op_id"]
    assert info["dsn_in_environment"] is False
    assert info["import_driver"] == "denied"  # defense in depth only (Python-level guard)


def test_every_statement_the_harness_executes_for_a_candidate_is_traced(cand_env, monkeypatch):
    """The harness is the only party with a connection; every statement it executed for the run appears in the
    trace, in order, per actor."""
    from gauntlet.actor import connection
    from gauntlet.engine.runner import execute_run
    executed: list[tuple[str, str]] = []
    real = connection.ActorConnection.start

    def recording(self, sql, params):
        executed.append((self.name, " ".join(sql.split())))
        return real(self, sql, params)

    monkeypatch.setattr(connection.ActorConnection, "start", recording)
    run = execute_run("S02_backfill_vs_legacy_writes", _register(GOOD), 1, isolation=TRUSTED, time_limit_s=180)
    assert run["infra_error"] is None, run["infra_error"]
    # 1. everything the harness executed is in the run's statement log, in order (client and protocol)
    logged = [(s["actor"], s["sql"]) for s in run["statement_log"]]
    assert executed and executed == logged
    # 2. every client statement in the log is in the hashed event trace, in order. A statement that waits on a
    #    lock is traced when it blocks ("blocked", possibly repeated) and when it completes ("unblocked");
    #    count it at its first "blocked" event.
    client = [(s["actor"], s["sql"]) for s in run["statement_log"] if not s["op_id"].startswith("P")]
    traced, waiting = [], set()
    for e in run["events"]:
        if e["kind"] == "stmt":
            traced.append((e["actor"], e["sql"]))
        elif e["kind"] == "blocked" and "attempt_id" in e and e["actor"] not in waiting:
            waiting.add(e["actor"])
            traced.append((e["actor"], e["sql"]))
        elif e["kind"] == "unblocked":
            waiting.discard(e["actor"])
    assert client and client == traced
    protocol = [s for s in run["statement_log"] if s["op_id"].startswith("P")]
    assert protocol and all("app_instances" in s["sql"] for s in protocol)  # instance registration only


def test_closing_an_actor_session_rolls_back_and_releases_its_locks(pg_available):
    """Crash emulation: the harness owns the session, so it must end it as a dying process did."""
    from gauntlet.actor.connection import ActorConnection
    from gauntlet.pg.provision import apply_legacy_schema, create_run_database, drop_run_database
    db = create_run_database()
    try:
        apply_legacy_schema(db.dsn, {1: "10.00"})
        a = ActorConnection(db.dsn, "a", 15000)
        b = ActorConnection(db.dsn, "b", 15000)
        with psycopg.connect(db.dsn, autocommit=True) as mon:
            for sql in ("BEGIN", "UPDATE orders SET total = 99 WHERE id = 1"):
                a.start(sql, None)
                assert a.wait(10).error is None
            b.start("UPDATE orders SET total = 50 WHERE id = 1", None)   # waits for a's row lock
            assert b.wait(0.5) is None and b.busy
            assert a.close(terminate=None) is True                     # idle: closed, transaction rolled back
            res = b.wait(10)                                           # the lock is released
            assert res is not None and res.error is None and res.rowcount == 1
            b.start("BEGIN", None); b.wait(10)
            b.start("UPDATE orders SET total = 51 WHERE id = 1", None); b.wait(10)
            c = ActorConnection(db.dsn, "c", 15000)
            c.start("UPDATE orders SET total = 52 WHERE id = 1", None)  # blocked behind b
            assert c.wait(0.5) is None
            terminated = []
            assert c.close(terminate=lambda pid: (terminated.append(pid),
                                                  mon.execute("SELECT pg_terminate_backend(%s)", (pid,)))) is True
            assert terminated == [c.backend_pid] and not c.busy        # a crash during a blocked statement
            b.close()
            assert mon.execute("SELECT total::text FROM orders WHERE id = 1").fetchone()[0] == "50.00"
            assert a.close() is True and c.close() is True             # idempotent
    finally:
        drop_run_database(db)


def test_snapshot_blocked_by_an_actor_lock_fails_promptly_instead_of_hanging(pg_available, monkeypatch):
    """Before the fix the monitor had no lock_timeout: a snapshot taken while a paused actor held a lock on
    `orders` waited forever (runs without a time limit, e.g. the benchmark, never ended)."""
    import time
    from gauntlet import config
    from gauntlet.engine.controller import Controller
    from gauntlet.engine.runner import open_monitor
    from gauntlet.pg.provision import (InfrastructureError, apply_legacy_schema, create_run_database,
                                       drop_run_database)
    monkeypatch.setattr(config, "MONITOR_LOCK_TIMEOUT_MS", 500)
    db = create_run_database()
    try:
        apply_legacy_schema(db.dsn, {1: "10.00"})
        with psycopg.connect(db.dsn) as holder, open_monitor(db.dsn) as monitor:
            holder.execute("LOCK TABLE orders IN ACCESS EXCLUSIVE MODE")  # held: the transaction stays open
            ctl = Controller("ref_a_trigger", db.dsn, monitor, {})
            t = time.monotonic()
            with pytest.raises(InfrastructureError, match="lock held by a paused actor"):
                ctl._snapshot("while locked")
            assert time.monotonic() - t < 10
            holder.rollback()
            ctl.pool.close()
    finally:
        drop_run_database(db)


# -- M1 / M2: one statement per request, bounded results -------------------------------------------------------
def _fresh_db():
    from gauntlet.pg.provision import apply_legacy_schema, create_run_database
    db = create_run_database()
    apply_legacy_schema(db.dsn, {1: "10.00", 2: "20.50"})
    return db


EQUIVALENCE = [
    ("SELECT id, total FROM orders ORDER BY id", None),
    ("SELECT id FROM orders WHERE false", None),
    ("SELECT total FROM orders WHERE id = %s", (2,)),
    ("SELECT %(a)s::numeric + %(b)s::numeric AS s", {"a": "1.25", "b": "2.50"}),
    ("UPDATE orders SET total = total + 1 WHERE id = %s", (1,)),
    ("UPDATE orders SET total = 0 WHERE id = %s", (99,)),
    ("INSERT INTO orders (id, total) VALUES (%s, %s::numeric) RETURNING id, total", (3, "7.00")),
    ("BEGIN", None),
    ("INSERT INTO orders (id, total) VALUES (4, 1.00)", None),
    ("COMMIT", None),
    ("INSERT INTO orders (id, total) VALUES (1, 1.00)", None),            # unique violation
    ("ALTER TABLE orders ADD COLUMN IF NOT EXISTS total_cents BIGINT", None),
    ("DO $$ BEGIN UPDATE orders SET total_cents = 1 WHERE id = 1; END $$", None),
    ("DELETE FROM orders WHERE id = %s RETURNING id", (4,)),
    ("SELECT response FROM processed_requests", None),
    ("ROLLBACK", None),                                                    # outside a transaction: a warning
]


def test_harness_execution_matches_psycopg_execute_statement_by_statement(pg_available):
    """Rows (exact types), row counts, errors and transaction state are identical to a plain execute()."""
    from psycopg.rows import dict_row
    from gauntlet.actor import wire
    from gauntlet.actor.connection import ActorConnection
    from gauntlet.pg.provision import drop_run_database
    a_db, b_db = _fresh_db(), _fresh_db()
    try:
        a = ActorConnection(a_db.dsn, "a", 15000)
        with psycopg.connect(b_db.dsn, autocommit=True) as b:
            for sql, params in EQUIVALENCE:
                a.start(sql, params)
                ra = a.wait(10)
                cur = b.cursor(row_factory=dict_row)
                try:
                    cur.execute(sql, params)
                    rb = (cur.fetchall() if cur.description else None, cur.rowcount, None)
                except psycopg.Error as exc:
                    rb = (None, None, exc.sqlstate)
                got = (wire.decode(ra.rows) if ra.rows is not None else None, ra.rowcount,
                       ra.error["sqlstate"] if ra.error else None)
                assert got == rb, sql
                assert ra.in_txn == (b.info.transaction_status != psycopg.pq.TransactionStatus.IDLE), sql
        a.close()
    finally:
        drop_run_database(a_db)
        drop_run_database(b_db)


@pytest.mark.parametrize("sql", ["SELECT 1; UPDATE orders SET total = 0",
                                 "UPDATE orders SET total = 0 WHERE id = 1; UPDATE orders SET total = 0 WHERE id = 2",
                                 "SELECT 1;\n-- comment\nDELETE FROM orders"])
def test_one_request_can_never_execute_several_statements(pg_available, sql):
    from gauntlet.actor.connection import ActorConnection
    from gauntlet.pg.provision import drop_run_database
    db = _fresh_db()
    try:
        with psycopg.connect(db.dsn, autocommit=True) as mon:
            before = mon.execute("SELECT id, total::text FROM orders ORDER BY id").fetchall()
            a = ActorConnection(db.dsn, "a", 15000)
            a.start(sql, None)
            res = a.wait(10)
            assert res.error is not None and res.error["sqlstate"] == "42601"  # rejected by the server
            assert mon.execute("SELECT id, total::text FROM orders ORDER BY id").fetchall() == before  # no effect
            a.start("SELECT 1 AS ok", None)  # the connection is still usable
            assert a.wait(10).rowcount == 1
            a.close()
            # positive control: the plain simple-protocol path WOULD run several statements
            with psycopg.connect(db.dsn, autocommit=True) as plain:
                plain.execute("SELECT 1; UPDATE orders SET total = 0 WHERE id = 1")
            assert mon.execute("SELECT total::text FROM orders WHERE id = 1").fetchone()[0] == "0.00"
    finally:
        drop_run_database(db)


def test_row_and_byte_budgets_stop_the_statement_without_buffering_it(pg_available, monkeypatch):
    import time
    from gauntlet import config
    from gauntlet.actor.connection import ActorConnection
    from gauntlet.pg.provision import drop_run_database
    monkeypatch.setattr(config, "MAX_RESULT_ROWS", 1000)
    monkeypatch.setattr(config, "MAX_RESULT_BYTES", 50_000)
    db = _fresh_db()
    try:
        a = ActorConnection(db.dsn, "a", 15000)
        t = time.monotonic()
        # ~50M rows if buffered. The series is in the select list so that the server streams it (ProjectSet): in
        # FROM it is a Function Scan, which PostgreSQL first materializes into a ~670 MB temporary file, so the
        # first row (and with it the harness's row budget) only arrives after about 10 s of disk writes and the
        # bound below then measures the host's disk, not the budget (audit finding FR-9).
        a.start("SELECT generate_series(1, 50000000) AS g", None)
        res = a.wait(30)
        assert res is not None and res.failure and "more than 1000 rows" in res.failure and res.rows is None
        assert time.monotonic() - t < 10  # stopped and cancelled early, not after producing 50M rows
        a.start("SELECT repeat('x', 200000) AS big", None)
        res = a.wait(10)
        assert res.failure and "larger than 50000 bytes" in res.failure
        a.start("SELECT '10.0.0.1'::inet AS ip", None)              # a type the wire format cannot carry exactly
        res = a.wait(10)
        assert res.failure and "cannot be sent" in res.failure
        a.start("SELECT 1 AS ok", None)                              # the session is idle and usable afterwards
        res = a.wait(10)
        assert res.rowcount == 1 and not a.in_txn()
        a.close()
    finally:
        drop_run_database(db)


def test_binding_matches_psycopg_for_parameter_types_and_formats(pg_available):
    """The values and the server-side types of bound parameters are the ones a psycopg cursor produces."""
    import datetime
    import decimal
    import uuid
    from psycopg.rows import dict_row
    from gauntlet.actor import wire
    from gauntlet.actor.connection import ActorConnection
    from gauntlet.pg.provision import drop_run_database
    cases = [
        ("SELECT %s AS v, pg_typeof(%s)::text AS t", lambda v: (v, v)),
        ("SELECT %(x)s AS v, pg_typeof(%(x)s)::text AS t", lambda v: {"x": v}),
        ("SELECT %b AS v", lambda v: (v,)), ("SELECT %t AS v", lambda v: (v,)),
    ]
    values = [1, -5, 2 ** 40, 2 ** 70, 1.5, decimal.Decimal("3.14"), True, "text", "é ✓ %", b"\x00ab", [1, 2, 3], ["a", "b"],
              datetime.date(2026, 5, 6), datetime.datetime(2026, 5, 6, 7, 8, 9),
              datetime.datetime(2026, 5, 6, 7, 8, 9, tzinfo=datetime.timezone.utc), uuid.UUID(int=7)]
    db = _fresh_db()
    try:
        a = ActorConnection(db.dsn, "a", 15000)
        with psycopg.connect(db.dsn, autocommit=True) as b:
            for sql, make in cases:
                for v in values:
                    params = make(v)
                    a.start(sql, params)
                    ra = a.wait(10)
                    try:
                        rb = (b.cursor(row_factory=dict_row).execute(sql, params).fetchall(), None)
                    except psycopg.Error as exc:
                        rb = (None, exc.sqlstate)
                    if ra.failure:       # a value the wire format cannot carry (not a binding question)
                        continue
                    got = (wire.decode(ra.rows) if ra.rows is not None else None, ra.error["sqlstate"] if ra.error else None)
                    assert got == rb, (sql, v)
        a.close()
    finally:
        drop_run_database(db)


@pytest.mark.parametrize("sql", ["COPY orders TO STDOUT", "COPY orders FROM STDIN",
                                 "COPY (SELECT generate_series(1, 2000000)) TO STDOUT"])
def test_copy_is_refused_and_the_session_stays_usable(pg_available, sql):
    """COPY to or from the client is not part of the statement protocol. It must end as an error of that one
    statement, change nothing, and leave the session idle (before, a COPY left the connection in copy mode and
    every later statement of that actor failed with 'another command is already in progress')."""
    import time
    from gauntlet.actor.connection import ActorConnection
    from gauntlet.pg.provision import drop_run_database
    db = _fresh_db()
    try:
        a = ActorConnection(db.dsn, "a", 15000)
        for in_transaction in (False, True):
            if in_transaction:
                a.start("BEGIN", None)
                assert a.wait(10).error is None
            t = time.monotonic()
            a.start(sql, None)
            res = a.wait(30)
            assert res is not None and res.error is not None and res.rows is None and res.failure is None
            assert "COPY" in res.error["message"]
            assert time.monotonic() - t < 20
            if in_transaction:           # the failed statement aborted the transaction; it can be rolled back
                a.start("ROLLBACK", None)
                assert a.wait(10).error is None
            a.start("SELECT count(*) AS n FROM orders", None)
            res = a.wait(10)
            assert res.error is None and res.rowcount == 1 and not a.in_txn()
        with psycopg.connect(db.dsn, autocommit=True) as mon:
            assert mon.execute("SELECT count(*) FROM orders").fetchone()[0] == 2  # nothing was copied in
        a.close()
    finally:
        drop_run_database(db)


@pytest.mark.parametrize("sql", ["-- only a comment", ";", "/* nothing */"])
def test_an_empty_statement_is_an_error_and_the_session_stays_usable(pg_available, sql):
    from gauntlet.actor.connection import ActorConnection
    from gauntlet.pg.provision import drop_run_database
    db = _fresh_db()
    try:
        a = ActorConnection(db.dsn, "a", 15000)
        a.start(sql, None)
        res = a.wait(10)
        assert res.error == {"sqlstate": None, "message": "the statement is empty"} and res.rows is None
        a.start("SELECT 1 AS ok", None)
        assert a.wait(10).rowcount == 1 and not a.in_txn()
        a.close()
    finally:
        drop_run_database(db)


def test_a_statement_that_fails_after_sending_rows_is_an_error_without_rows(pg_available):
    from gauntlet.actor.connection import ActorConnection
    from gauntlet.pg.provision import drop_run_database
    db = _fresh_db()
    try:
        a = ActorConnection(db.dsn, "a", 15000)
        a.start("SELECT g, 1 / (g - 500) AS q FROM generate_series(1, 2000) g", None)  # division by zero at row 500
        res = a.wait(10)
        assert res.error is not None and res.error["sqlstate"] == "22012" and res.rows is None and res.rowcount is None
        a.start("SELECT 1 AS ok", None)
        assert a.wait(10).rowcount == 1
        a.close()
    finally:
        drop_run_database(db)


def test_actor_connection_timeout_is_bounded(monkeypatch):
    import socket
    import time
    from gauntlet import config
    from gauntlet.actor.connection import ActorConnection
    monkeypatch.setattr(config, "ACTOR_CONNECT_TIMEOUT_S", 2)
    silent = socket.socket()
    silent.bind(("127.0.0.1", 0))
    silent.listen(1)  # accepts the TCP connection but never speaks the PostgreSQL protocol
    port = silent.getsockname()[1]
    t = time.monotonic()
    try:
        with pytest.raises(psycopg.OperationalError):
            ActorConnection(f"postgresql://u:p@127.0.0.1:{port}/postgres", "a", 15000)
        assert time.monotonic() - t < 10
    finally:
        silent.close()


def _candidate_with_expand(cand_env, name: str, body: str) -> str:
    import json
    d = cand_env / name
    shutil.copytree(GOOD, d)
    src = (d / "candidate.py").read_text(encoding="utf-8")
    src += "\n\nclass Variant(Implementation):\n    def migrator_expand(self, db, a):\n" + \
           "\n".join("        " + line for line in body.strip().splitlines()) + "\n"
    (d / "candidate.py").write_text(src, encoding="utf-8")
    m = json.loads((d / "gauntlet_candidate.json").read_text(encoding="utf-8"))
    m["name"], m["entry"]["class"] = name.replace("_", "-"), "Variant"
    (d / "gauntlet_candidate.json").write_text(json.dumps(m), encoding="utf-8")
    return _register(d)


def test_candidate_multi_statement_request_is_rejected_and_traced(cand_env):
    from gauntlet.engine.runner import execute_run
    impl = _candidate_with_expand(cand_env, "multi_stmt",
                                  'db.execute("SELECT 1; UPDATE orders SET total = 0")\nreturn {}')
    run = execute_run("S01_baseline_migration", impl, 1, isolation=TRUSTED, time_limit_s=180)
    expand = next(a for a in run["attempts"] if a["op"] == "expand")
    assert expand["status"] == "failed" and expand["error"]["sqlstate"] == "42601"
    traced = [e for e in run["events"] if e["kind"] == "stmt" and e["sql"].startswith("SELECT 1; UPDATE")]
    assert len(traced) == 1 and traced[0]["error"]["sqlstate"] == "42601"
    assert all(r["total"] != "0.00" for r in run["snapshots"][-1]["rows"])  # the hidden UPDATE never ran


def test_candidate_huge_result_is_a_candidate_execution_failure(cand_env, monkeypatch):
    from gauntlet import config
    from gauntlet.engine.runner import execute_run
    monkeypatch.setattr(config, "MAX_RESULT_ROWS", 1000)
    impl = _candidate_with_expand(cand_env, "huge_result",
                                  'db.query("SELECT g FROM generate_series(1, 50000000) g")\nreturn {}')
    run = execute_run("S01_baseline_migration", impl, 1, isolation=TRUSTED, time_limit_s=180)
    assert run["verdict"] != "pass" and run["error_class"] == "candidate_execution_failure", run["infra_error"]
    assert "more than 1000 rows" in run["infra_error"]
