"""Regression tests for the full-audit hardening pass (no database needed).

Each test fails on the code before the corresponding fix:
* SQL classification: leading comments, CTE writes split across lines, MERGE / CALL / DO / COPY;
* actor protocol: the harness owns the database connection (A-01); gate / respond messages are
  validated against the pending state, and statement class / transaction state are the harness's own;
* cleanup: one failing kill or release never prevents the remaining cleanup;
* sandbox: concurrent cleanup (watchdog thread vs controller thread) closes the job handle exactly once.
"""

from __future__ import annotations

import json
import pathlib
import queue
import sqlite3
import sys
import threading
from decimal import Decimal

import pytest

from gauntlet.actor.child import classify
from gauntlet.actor.proxy import ActorFailure, ActorProxy, ActorState, ProcessPool
from gauntlet.pg.provision import InfrastructureError

EVIDENCE = pathlib.Path(__file__).resolve().parents[2] / "evidence"


# -- SQL classification --------------------------------------------------------------------------------
@pytest.mark.parametrize("sql,cls", [
    ("-- backfill one batch\nUPDATE orders SET total_cents = 1 WHERE id = 1", "write"),
    ("/* note */ INSERT INTO orders (id) VALUES (1)", "write"),
    ("  /* a */ -- b\n  DELETE FROM orders", "write"),
    ("WITH x AS (SELECT 1)\nUPDATE\norders SET total_cents = 2", "write"),
    ("WITH x AS (SELECT 1)\nINSERT\tINTO orders (id) VALUES (3)", "write"),
    ("MERGE INTO orders o USING s ON o.id = s.id WHEN MATCHED THEN UPDATE SET total_cents = 1", "write"),
    ("CALL do_migration()", "write"),
    ("DO $$ BEGIN UPDATE orders SET total_cents = 0; END $$", "write"),
    ("COPY orders FROM STDIN", "write"),
    ("-- comment\nALTER TABLE orders ADD COLUMN x int", "ddl"),
    ("COMMIT;", "commit"),
    ("-- c\nBEGIN", "begin"),
    ("SELECT 1", "read"),
    ("WITH x AS (SELECT 1) SELECT * FROM x", "read"),
    ("", "other"),
    ("-- only a comment", "other"),
])
def test_classification_is_robust_to_comments_ctes_and_write_statements(sql, cls):
    assert classify(sql) == cls


def test_classification_of_every_recorded_statement_is_unchanged():
    """The frozen evidence's trace hashes depend on the recorded classes: they must not change."""
    seen = {}
    for f in sorted(EVIDENCE.glob("*.sqlite3")):
        c = sqlite3.connect(f.resolve().as_uri() + "?mode=ro&immutable=1", uri=True)
        try:
            for (dj,) in c.execute("select data_json from runs"):
                for e in json.loads(dj).get("events", []):
                    if "sql" in e and "cls" in e:
                        seen.setdefault(e["sql"], set()).add(e["cls"])
        finally:
            c.close()
    assert len(seen) >= 40
    assert {s: classify(s) for s in seen} == {s: next(iter(c)) for s, c in seen.items()}


# -- actor protocol: the harness owns the connection (A-01) --------------------------------------------------
class _Stdin:
    def __init__(self):
        self.sent = []

    def write(self, s):
        self.sent.extend(json.loads(line) for line in s.splitlines() if line.strip())

    def flush(self):
        pass


class _Proc:
    def __init__(self):
        self.stdin = _Stdin()

    def poll(self):
        return None


class _Child:
    def __init__(self, *msgs):
        self.q = queue.Queue()
        for m in msgs:
            self.q.put(m)
        self.proc = _Proc()
        self.stderr_tail = []
        self.sandbox = None


class _Conn:
    """Stands in for the harness-side ActorConnection (no database)."""

    def __init__(self, result=None, in_txn=False, close_ok=True):
        from gauntlet.actor import wire
        from gauntlet.actor.connection import StatementResult
        self.result = result or StatementResult(rows=[wire.encode({"total": Decimal("12.50")})], rowcount=1,
                                                error=None, exception=None, in_txn=False)
        self._in_txn, self.started, self.busy, self.backend_pid = in_txn, [], False, None
        self.close_ok, self.closed_calls = close_ok, 0

    def close(self, terminate=None, timeout=10):
        self.closed_calls += 1
        return self.close_ok

    def in_txn(self):
        return self._in_txn

    def start(self, sql, params):
        self.started.append((sql, params))

    def wait(self, timeout):
        return self.result


def _gate(op_id="L001.a1", seq=7, sql="SELECT total FROM orders WHERE id = %s", params=None, **extra):
    from gauntlet.actor import wire
    return {"type": "gate", "op_id": op_id, "seq": seq, "sql": sql,
            "params": wire.encode([1] if params is None else params), **extra}


def _proxy(*msgs, conn=None, op_id="L001.a1"):
    p = ActorProxy("v2-a", "v2", "ref_a_trigger", "dsn", blocking_probe=lambda pid: [])
    p.child, p.conn, p.backend_pid, p.state, p._loaded = _Child(*msgs), conn or _Conn(), None, ActorState.IDLE, True
    p.submit(op_id, "v2_get_order", {"order_id": 1})
    return p


def test_granted_statement_is_executed_by_the_harness_and_its_result_sent_to_the_actor():
    from gauntlet.actor import wire
    p = _proxy(_gate(params=[Decimal("1.50"), 2]),
               {"type": "respond", "op_id": "L001.a1", "outcome": {"status": "ok", "result": {}}})
    assert p.state == ActorState.AT_GATE and p.gate.cls == "read"
    rec = p.grant()
    assert p.conn.started == [("SELECT total FROM orders WHERE id = %s", [Decimal("1.50"), 2])]
    assert rec == {"sql": "SELECT total FROM orders WHERE id = %s", "params": ["1.50", 2], "cls": "read",
                   "in_txn": False, "rowcount": 1, "error": None}
    result = [m for m in p.child.proc.stdin.sent if m["type"] == "result"]
    assert len(result) == 1 and result[0]["seq"] == 7
    assert wire.decode(result[0]["rows"]) == [{"total": Decimal("12.50")}]  # exact type survives the boundary
    assert p.state == ActorState.AT_RESPOND


def test_the_actor_receives_no_database_credentials():
    p = ActorProxy("v2-a", "v2", "ref_a_trigger", "postgresql://u:secret@h/db", blocking_probe=lambda pid: [])

    class _Pool:
        def take(self):
            return _Child()
    p._pool = _Pool()
    import gauntlet.actor.proxy as px
    orig = px.ActorConnection
    px.ActorConnection = lambda dsn, name, lt: _Conn()
    try:
        p.spawn()
    finally:
        px.ActorConnection = orig
    (init,) = p.child.proc.stdin.sent
    assert init["type"] == "init" and "dsn" not in init and "secret" not in json.dumps(init)


def test_statement_class_and_transaction_state_come_from_the_harness_not_the_actor():
    # An actor that labels a write as a read (to dodge a before_write fault point) is ignored.
    p = _proxy(_gate(sql="UPDATE orders SET total_cents = 1", cls="read", in_txn=False), conn=_Conn(in_txn=True))
    assert p.gate.cls == "write" and p.gate.in_txn is True


@pytest.mark.parametrize("bad", [
    {"type": "gate", "op_id": "L001.a1", "seq": 9},                                      # missing sql / params
    _gate(op_id="OTHER", seq=9),                                                          # foreign operation
    _gate(seq=7),                                                                         # seq not increasing
    _gate(seq=9, sql="   "),                                                              # empty statement
    {**_gate(seq=9), "params": {"order_id": 1}},                                          # untagged dict
    {**_gate(seq=9), "params": {"\x00gauntlet": "evil", "v": 1}},                         # unknown tag
    {**_gate(seq=9), "params": 5},                                                        # not a sequence/mapping
    {"type": "stmt_done", "op_id": "L001.a1", "seq": 8, "rowcount": 1, "error": None},   # forged completion
    {"type": "respond", "op_id": "OTHER", "outcome": {"status": "ok"}},
    {"type": "respond", "op_id": "L001.a1", "outcome": {"status": "maybe"}},
    {"type": "respond", "op_id": "L001.a1"},
    {"type": "hello"},
])
def test_malformed_or_forged_actor_messages_are_protocol_failures(bad):
    p = _proxy(_gate(), bad)
    with pytest.raises(ActorFailure, match="protocol violation"):
        p.grant()


def test_a_result_refused_by_the_harness_is_a_recorded_actor_failure_and_never_reaches_the_actor():
    from gauntlet.actor.connection import StatementResult
    res = StatementResult(rows=None, rowcount=None, error=None, exception=None, in_txn=False,
                          failure="the result has more than 100000 rows (harness limit MAX_RESULT_ROWS)")
    p = _proxy(_gate(), conn=_Conn(result=res))
    with pytest.raises(ActorFailure, match="refused: the result has more than 100000 rows"):
        p.grant()
    assert not [m for m in p.child.proc.stdin.sent if m["type"] == "result"]
    assert p.progress.stmt_log[-1]["error"]["message"].startswith("result refused by the harness")


def test_deeply_nested_parameters_are_a_protocol_failure_not_a_harness_crash():
    deep = [1]
    for _ in range(100_000):
        deep = [deep]
    p = ActorProxy("v2-a", "v2", "ref_a_trigger", "dsn", blocking_probe=lambda pid: [])
    p.child, p.conn, p.backend_pid, p.state, p._loaded = (_Child({"type": "gate", "op_id": "L001.a1", "seq": 1,
                                                                  "sql": "SELECT %s", "params": deep}),
                                                           _Conn(), None, ActorState.IDLE, True)
    with pytest.raises(ActorFailure, match="malformed statement parameters"):
        p.submit("L001.a1", "v2_get_order", {"order_id": 1})


# -- parameter binding (gauntlet.actor.connection.bind): no database --------------------------------------
@pytest.mark.parametrize("sql,params,expected", [
    ("SELECT 1", None, ("SELECT 1", None, None)),
    ("SELECT '100%' AS p, '%s' AS q", None, ("SELECT '100%' AS p, '%s' AS q", None, None)),   # no parameters: untouched
    ("SELECT %s, %s", (1, "a"), ("SELECT $1, $2", [1, "a"], ["s", "s"])),
    ("SELECT %s", [7], ("SELECT $1", [7], ["s"])),
    ("SELECT %b, %t, %s", (1, 2, 3), ("SELECT $1, $2, $3", [1, 2, 3], ["b", "t", "s"])),
    ("SELECT %(a)s, %(b)s, %(a)s", {"a": 1, "b": 2, "unused": 3}, ("SELECT $1, $2, $1", [1, 2], ["s", "s"])),
    ("SELECT %(b)t, %(a)b", {"a": 1, "b": 2}, ("SELECT $1, $2", [2, 1], ["t", "b"])),
    ("SELECT '100%%', %s", (5,), ("SELECT '100%', $1", [5], ["s"])),
    ("SELECT '%%s'", (), ("SELECT '%s'", [], [])),
    ("SELECT 1", (), ("SELECT 1", [], [])),
    ("SELECT 1", {"a": 1}, ("SELECT 1", [], [])),
    ("SELECT 5 %", (), ("SELECT 5 %", [], [])),                                              # a lone % at the end
    ("SELECT %s -- é ✓", ("x",), ("SELECT $1 -- é ✓", ["x"], ["s"])),
    ("%s%s%s%s%s%s%s%s%s%s%s", tuple(range(11)), ("$1$2$3$4$5$6$7$8$9$10$11", list(range(11)), ["s"] * 11)),
])
def test_bind_rewrites_placeholders_and_orders_parameters(sql, params, expected):
    from gauntlet.actor.connection import bind
    query, values, formats = bind(sql, params)
    assert (query, values, [f.value for f in formats] if formats is not None else None) == expected


@pytest.mark.parametrize("sql,params,exc", [
    ("SELECT %s, %s", (1,), "ProgrammingError"), ("SELECT %s", (1, 2), "ProgrammingError"), ("SELECT 1", [1], "ProgrammingError"),
    ("SELECT %(a)s", {}, "ProgrammingError"), ("SELECT %(a)s, %s", {"a": 1}, "ProgrammingError"),
    ("SELECT %s, %(a)s", (1,), "ProgrammingError"), ("SELECT %d", (1,), "ProgrammingError"), ("SELECT '%'", (), "ProgrammingError"),
    ("SELECT %(a", {"a": 1}, "ProgrammingError"), ("SELECT %(a)", {"a": 1}, "ProgrammingError"), ("SELECT %(a)d", {"a": 1}, "ProgrammingError"),
    ("SELECT %(a)s, %(a)b", {"a": 1}, "ProgrammingError"), ("SELECT 5 %", (1,), "ProgrammingError"),
    ("SELECT %s", "x", "TypeError"), ("SELECT %s", b"x", "TypeError"), ("SELECT %s", 5, "TypeError"),
    ("SELECT %s", {"a": 1}, "TypeError"), ("SELECT %(a)s", (1,), "TypeError"),
])
def test_bind_rejects_placeholders_and_parameters_that_do_not_fit(sql, params, exc):
    """The exception types are the ones psycopg's own cursors raise for the same mistakes, so an implementation
    sees the same kind of failure (a database-driver error, or a TypeError) as it would with a plain cursor."""
    import psycopg
    from gauntlet.actor.connection import bind
    with pytest.raises({"ProgrammingError": psycopg.ProgrammingError, "TypeError": TypeError}[exc]):
        bind(sql, params)


def test_the_statement_path_uses_only_public_psycopg_interfaces():
    """The harness's statement execution must not subclass psycopg's cursor or reach into its private
    attributes (it once overrode a private cursor method, which tied it to psycopg's internals)."""
    import ast
    import inspect
    from gauntlet.actor import connection
    tree = ast.parse(inspect.getsource(connection))
    assert not [n.name for n in ast.walk(tree) if isinstance(n, ast.ClassDef)
                and any("Cursor" in ast.unparse(b) for b in n.bases)]
    private = sorted({n.attr for n in ast.walk(tree) if isinstance(n, ast.Attribute) and n.attr.startswith("_")
                      and not n.attr.endswith("__")  # Python's own dunder attributes, e.g. type(exc).__name__
                      and not (isinstance(n.value, ast.Name) and n.value.id == "self")})
    assert private == [], private
    imported = sorted({a.name for n in ast.walk(tree) if isinstance(n, ast.ImportFrom) and (n.module or "").startswith("psycopg")
                       for a in n.names} | {n.module for n in ast.walk(tree) if isinstance(n, ast.ImportFrom)
                                             and (n.module or "").startswith("psycopg")})
    assert not [x for x in imported if x.startswith("_") or "._" in x or x == "generators"], imported


def test_reader_survives_a_deeply_nested_protocol_line():
    import io
    from gauntlet.actor.proxy import ChildProcess
    c = ChildProcess.__new__(ChildProcess)
    c.sandbox, c.q = None, queue.Queue()
    c.proc = type("P", (), {"stdout": io.StringIO("[" * 100_000 + "]" * 100_000 + "\n" + '{"type": "hello"}\n')})()
    c._reader()
    msgs = [c.q.get_nowait() for _ in range(c.q.qsize())]
    assert [m["type"] for m in msgs] == ["garbage", "hello", "eof"]  # the thread kept reading


@pytest.mark.parametrize("sandboxed", [False, True])
def test_a_protocol_line_that_is_not_a_json_object_is_garbage(sandboxed):
    """Valid JSON of another type must never reach the proxy as a message: it has no "type", and the proxy would
    fail with an AttributeError (a harness exception) instead of reporting the actor's protocol violation. A
    deeply nested array is the same case on platforms whose stack is large enough to parse it."""
    import io
    from gauntlet.actor.proxy import ChildProcess
    lines = ["[1]", "123", "null", '"text"', "true", "[" * 2000 + "]" * 2000, "not json", '{"type": "hello"}']
    c = ChildProcess.__new__(ChildProcess)
    c.sandbox, c.q = (object() if sandboxed else None), queue.Queue()
    c.proc = type("P", (), {"stdout": io.StringIO("".join(x + "\n" for x in lines))})()
    c._reader()
    msgs = [c.q.get_nowait() for _ in range(c.q.qsize())]
    assert all(isinstance(m, dict) for m in msgs)
    assert [m["type"] for m in msgs] == ["garbage"] * 7 + ["hello", "eof"]
    # and the proxy reports garbage as the actor's failure
    p = ActorProxy("v2-a", "v2", "ref_a_trigger", "dsn", blocking_probe=lambda pid: [])
    p.child, p._loaded = _Child(msgs[0]), True
    with pytest.raises(ActorFailure, match="died or broke protocol"):
        p._recv(timeout=1)


def test_a_connection_that_cannot_be_shut_down_is_recorded():
    from gauntlet.engine.controller import Controller
    p = ActorProxy("v2-a", "v2", "ref_a_trigger", "dsn", blocking_probe=lambda pid: [])
    p.child, p.conn = None, _Conn(close_ok=False)
    p.kill()
    assert p.conn.closed_calls == 1 and p.cleanup_errors and "could not be stopped" in p.cleanup_errors[0]
    ctl = Controller.__new__(Controller)
    ctl.actors, ctl.pool, ctl.notes = {"v2-a": p}, _FakePool(), []
    ctl.shutdown()
    assert any(n.startswith("cleanup: actor v2-a") for n in ctl.notes)


def test_protocol_failures_are_infrastructure_errors_never_passes():
    assert issubclass(ActorFailure, InfrastructureError)


# -- wire codec -------------------------------------------------------------------------------------------------
def test_wire_round_trip_preserves_exact_types():
    import datetime as dt
    import uuid
    from gauntlet.actor import wire
    v = [None, True, 0, -5, 1.5, "x", Decimal("19.99"), Decimal("-0.00"), [1, [2]], (3, 4),
         {"a": {"\x00gauntlet": "not a tag here", "v": 1}}, dt.datetime(2026, 1, 2, 3, 4, 5), dt.date(2026, 1, 2),
         dt.time(1, 2, 3), dt.timedelta(days=1, seconds=2), uuid.UUID(int=7), b"\x00\xff"]
    out = wire.decode(json.loads(json.dumps(wire.encode(v))))
    assert out[:9] == v[:9] and out[9] == [3, 4] and out[10:] == v[10:]
    assert type(out[6]) is Decimal and type(out[3]) is int and out[1] is True


@pytest.mark.parametrize("bad", [{"a": 1}, {"\x00gauntlet": "decimal", "v": 5}, {"\x00gauntlet": "decimal", "v": "x"},
                                 {"\x00gauntlet": "exec", "v": "print(1)"}, {"\x00gauntlet": "dict", "v": [["a"]]}])
def test_wire_rejects_malformed_values(bad):
    from gauntlet.actor import wire
    with pytest.raises(wire.WireError):
        wire.decode(bad)


def test_wire_refuses_unrepresentable_values():
    from gauntlet.actor import wire
    with pytest.raises(wire.WireError):
        wire.encode(object())


# -- the actor side: a request channel only --------------------------------------------------------------------
class _Chan:
    def __init__(self, *replies):
        self.sent, self.replies = [], list(replies)

    def send(self, msg):
        self.sent.append(json.loads(json.dumps(msg)))

    def recv(self):
        return self.replies.pop(0)


def _result(seq, rows=None, rowcount=0, error=None, in_txn=False):
    from gauntlet.actor import wire
    return {"type": "result", "seq": seq, "rows": wire.encode(rows) if rows is not None else None,
            "rowcount": rowcount, "error": error, "exception": None, "in_txn": in_txn}


def test_gated_db_sends_requests_and_decodes_results():
    from gauntlet.actor.child import GatedDb
    db = GatedDb(_Chan(_result(1, [{"total": Decimal("10.00"), "id": 3}], 1)))
    db.op_id = "L001.a1"
    assert db.query("SELECT total, id FROM orders WHERE total > %s", (Decimal("1.00"),)) == \
        [{"total": Decimal("10.00"), "id": 3}]
    (gate,) = db._chan.sent
    assert gate["type"] == "gate" and gate["seq"] == 1 and "cls" not in gate and "in_txn" not in gate


def test_gated_db_raises_database_errors_and_rolls_back_through_the_gate():
    from gauntlet.actor.child import DbError, GatedDb
    chan = _Chan(_result(1, in_txn=True),
                 _result(2, error={"sqlstate": "23505", "message": "duplicate key"}, in_txn=True),
                 _result(3, in_txn=False))
    db = GatedDb(chan)
    db.op_id = "L001.a1"
    with pytest.raises(DbError) as ei:
        with db.transaction():
            db.execute("INSERT INTO orders (id, total) VALUES (1, 1)")
    assert ei.value.sqlstate == "23505"
    assert [m["sql"] for m in chan.sent] == ["BEGIN", "INSERT INTO orders (id, total) VALUES (1, 1)", "ROLLBACK"]
    assert db._in_txn() is False


def test_gated_db_holds_no_connection():
    from gauntlet.actor.child import GatedDb
    db = GatedDb(_Chan())
    assert set(vars(db)) == {"_chan", "op_id", "_seq", "_in_txn_flag"}


def test_the_actor_module_does_not_load_a_database_driver():
    import subprocess
    backend = pathlib.Path(__file__).resolve().parents[1]
    code = ("import sys; import gauntlet.actor.child, gauntlet.impls.registry, gauntlet.candidates.loader; "
            "print(sorted(m for m in sys.modules if m.split('.')[0] in ('psycopg', 'psycopg_binary', 'psycopg_c')))")
    out = subprocess.run([sys.executable, "-c", code], cwd=backend, capture_output=True, text=True, check=True)
    assert out.stdout.strip() == "[]"


# -- cleanup robustness ---------------------------------------------------------------------------------------
class _Killable:
    def __init__(self, fail=False):
        self.fail, self.killed = fail, 0

    def kill(self):
        self.killed += 1
        if self.fail:
            raise OSError("simulated kill failure")


def test_pool_close_kills_every_child_even_if_one_kill_fails():
    pool = ProcessPool(0)
    kids = [_Killable(), _Killable(fail=True), _Killable()]
    pool._free = list(kids)
    with pytest.raises(OSError):
        pool.close()
    assert [k.killed for k in kids] == [1, 1, 1]
    pool.close()  # idempotent: nothing left, nothing killed twice
    assert [k.killed for k in kids] == [1, 1, 1]


class _FakeProxy:
    def __init__(self, name, fail_exit=False, fail_kill=False):
        self.name, self.fail_exit, self.fail_kill = name, fail_exit, fail_kill
        self.state, self.child, self.exited, self.killed = ActorState.IDLE, None, 0, 0

    def exit(self):
        self.exited += 1
        if self.fail_exit:
            raise OSError("simulated exit failure")
        self.state = ActorState.DEAD

    def kill(self):
        self.killed += 1
        if self.fail_kill:
            raise OSError("simulated kill failure")


class _FakePool:
    closed = 0

    def close(self):
        self.closed += 1


def test_controller_shutdown_continues_after_a_failed_exit_and_kill():
    from gauntlet.engine.controller import Controller
    ctl = Controller.__new__(Controller)
    a, b, c = _FakeProxy("a", fail_exit=True, fail_kill=True), _FakeProxy("b"), _FakeProxy("c")
    ctl.actors, ctl.pool, ctl.notes = {"a": a, "b": b, "c": c}, _FakePool(), []
    ctl.shutdown()  # must not raise
    assert (a.exited, a.killed, b.exited, c.exited, ctl.pool.closed) == (1, 1, 1, 1, 1)
    assert any("a" in n and "simulated kill failure" in n for n in ctl.notes)


# -- sandbox: concurrent cleanup -----------------------------------------------------------------------------------
def test_concurrent_sandbox_cleanup_closes_the_job_exactly_once(tmp_path):
    from gauntlet.candidates.sandbox import ActorSandbox, IsolationConfig

    class _Job:
        closes = 0
        lock = threading.Lock()

        def close(self):
            with self.lock:
                _Job.closes += 1

        def terminate(self):
            pass

    sb = ActorSandbox(IsolationConfig(trusted_environment=True), pythonpath=str(tmp_path))
    sb._job = _Job()
    start = threading.Barrier(8)
    errors = []

    def worker():
        start.wait()
        try:
            sb.cleanup()
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert errors == [] and _Job.closes == 1 and not sb.workspace.exists()


@pytest.mark.skipif(not sys.platform.startswith("win"), reason="Windows job objects")
def test_job_handle_is_closed_once_under_concurrent_close(monkeypatch):
    from gauntlet.candidates import sandbox as sbmod
    job = sbmod._Job(sbmod.IsolationConfig(trusted_environment=True))
    real, calls = sbmod._k32.CloseHandle, []

    def counting(h):
        calls.append(h)
        return real(h)

    monkeypatch.setattr(sbmod._k32, "CloseHandle", counting)
    start = threading.Barrier(8)
    threads = [threading.Thread(target=lambda: (start.wait(), job.close())) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len(calls) == 1 and job.handle is None
