"""Actor subprocess: hosts one implementation instance behind statement gates.

Protocol (JSON lines; parent -> child on stdin, child -> parent on stdout):

    parent -> {"type": "init", "impl", "dsn", "name", "lock_timeout_ms"}
    child  -> {"type": "hello", "backend_pid": int, "os_pid": int}
    parent -> {"type": "op", "op_id": str, "method": str, "args": {...}}
    child  -> {"type": "gate", "op_id", "seq", "sql", "params", "cls", "in_txn"}
    parent -> {"type": "grant"}
    child  -> {"type": "stmt_done", "op_id", "seq", "rowcount", "error"}
    ...
    child  -> {"type": "respond", "op_id", "outcome": {...}}
    parent -> {"type": "grant"}          (response delivered; or the parent kills us)
    parent -> {"type": "exit"}

Every database statement (including BEGIN/COMMIT/ROLLBACK) waits for a grant,
so the parent fully controls the interleaving of statements across actors.
The child never decides scheduling. Implementation code runs only here,
never in the harness or API process.
"""

from __future__ import annotations

import json
import os
import sys
import traceback
from contextlib import contextmanager
from decimal import Decimal
from typing import Any

import psycopg
from psycopg.rows import dict_row

from gauntlet.impls.base import AppError


def classify(sql: str) -> str:
    head = sql.lstrip().split(None, 1)[0].upper() if sql.strip() else ""
    if head == "SELECT":
        return "read"
    if head == "WITH":
        upper = sql.upper()
        return "write" if any(k in upper for k in ("INSERT ", "UPDATE ", "DELETE ")) else "read"
    if head in ("INSERT", "UPDATE", "DELETE"):
        return "write"
    if head in ("ALTER", "CREATE", "DROP", "TRUNCATE"):
        return "ddl"
    if head in ("BEGIN", "START"):
        return "begin"
    if head in ("COMMIT", "END"):
        return "commit"
    if head == "ROLLBACK":
        return "rollback"
    return "other"


def _jsonable(v: Any) -> Any:
    if isinstance(v, Decimal):
        return str(v)
    if isinstance(v, (list, tuple)):
        return [_jsonable(x) for x in v]
    if isinstance(v, dict):
        return {k: _jsonable(x) for k, x in v.items()}
    return v


class Channel:
    def __init__(self) -> None:
        self._in = sys.stdin
        self._out = sys.stdout
        # Anything an implementation prints goes to stderr, never into the protocol.
        sys.stdout = sys.stderr

    def send(self, msg: dict) -> None:
        self._out.write(json.dumps(msg, default=str) + "\n")
        self._out.flush()

    def recv(self) -> dict:
        line = self._in.readline()
        if not line:
            raise SystemExit(0)  # parent went away
        return json.loads(line)


class GatedDb:
    """The ``Db`` handed to implementations. Each statement waits for a grant."""

    def __init__(self, conn: psycopg.Connection, chan: Channel):
        self._conn = conn
        self._chan = chan
        self.op_id: str | None = None
        self._seq = 0

    def _in_txn(self) -> bool:
        return self._conn.info.transaction_status != psycopg.pq.TransactionStatus.IDLE

    def _run(self, sql: str, params: Any = None):
        self._seq += 1
        seq = self._seq
        self._chan.send({
            "type": "gate", "op_id": self.op_id, "seq": seq,
            "sql": " ".join(sql.split()), "params": _jsonable(params),
            "cls": classify(sql), "in_txn": self._in_txn(),
        })
        msg = self._chan.recv()
        if msg.get("type") != "grant":
            raise RuntimeError(f"protocol violation: expected grant, got {msg}")
        cur = self._conn.cursor(row_factory=dict_row)
        try:
            cur.execute(sql, params)
        except psycopg.Error as exc:
            self._chan.send({
                "type": "stmt_done", "op_id": self.op_id, "seq": seq, "rowcount": None,
                "error": {"sqlstate": exc.sqlstate, "message": str(exc).strip().splitlines()[0] if str(exc) else ""},
            })
            raise
        self._chan.send({"type": "stmt_done", "op_id": self.op_id, "seq": seq,
                         "rowcount": cur.rowcount, "error": None})
        return cur

    def query(self, sql: str, params: Any = None) -> list[dict]:
        cur = self._run(sql, params)
        return cur.fetchall() if cur.description else []

    def one(self, sql: str, params: Any = None) -> dict | None:
        rows = self.query(sql, params)
        return rows[0] if rows else None

    def execute(self, sql: str, params: Any = None) -> int:
        return self._run(sql, params).rowcount

    @contextmanager
    def transaction(self):
        self._run("BEGIN")
        try:
            yield
        except BaseException:
            if self._in_txn() and not self._conn.closed:
                try:
                    self._run("ROLLBACK")
                except psycopg.Error:
                    pass
            raise
        else:
            self._run("COMMIT")

    def ensure_idle(self) -> None:
        """Roll back a transaction an implementation left open after an error."""
        if self._in_txn() and not self._conn.closed:
            self._run("ROLLBACK")


def main() -> None:
    # Processes may be pre-started (warm pool); identity arrives in an init message.
    from gauntlet.impls.registry import load_implementation

    chan = Channel()
    init = chan.recv()
    if init.get("type") != "init":
        chan.send({"type": "fatal", "error": f"expected init, got {init}"})
        return
    # The harness's own connection is opened before the implementation is loaded (an external candidate's
    # sandbox guard is installed during loading and must not affect the harness connection).
    conn = psycopg.connect(init["dsn"], autocommit=True, application_name=f"gauntlet:{init['name']}")
    conn.execute(f"SET lock_timeout = {int(init['lock_timeout_ms'])}")
    try:
        impl = load_implementation(init["impl"], init.get("sandbox"))
    except Exception as exc:  # e.g. a candidate that fails to import: report, never run
        chan.send({"type": "fatal", "error": f"could not load implementation: {type(exc).__name__}: {exc}"})
        conn.close()
        return
    chan.send({"type": "hello", "backend_pid": conn.info.backend_pid, "os_pid": os.getpid()})
    db = GatedDb(conn, chan)

    while True:
        msg = chan.recv()
        if msg["type"] == "exit":
            conn.close()
            return
        if msg["type"] != "op":
            chan.send({"type": "fatal", "error": f"unexpected message {msg}"})
            return
        db.op_id = msg["op_id"]
        method = getattr(impl, msg["method"], None)
        if method is None:
            outcome = {"status": "error", "error": {"kind": "app", "code": "unsupported_operation",
                                                     "message": msg["method"]}}
        else:
            try:
                result = method(db, msg["args"])
                outcome = {"status": "ok", "result": _jsonable(result)}
            except AppError as exc:
                outcome = {"status": "error", "error": {"kind": "app", "code": exc.code, "message": exc.message}}
            except psycopg.Error as exc:
                outcome = {"status": "error", "error": {"kind": "db", "code": "db_error",
                                                         "sqlstate": exc.sqlstate,
                                                         "message": str(exc).strip().splitlines()[0] if str(exc) else ""}}
            except Exception as exc:  # implementation bug (not harness): still a client-visible error
                outcome = {"status": "error", "error": {"kind": "exception", "code": type(exc).__name__,
                                                         "message": str(exc),
                                                         "traceback": traceback.format_exc(limit=5)}}
            try:
                db.ensure_idle()
            except psycopg.Error:
                pass
        chan.send({"type": "respond", "op_id": db.op_id, "outcome": outcome})
        grant = chan.recv()
        if grant.get("type") == "exit":
            conn.close()
            return
        db.op_id = None


if __name__ == "__main__":
    main()
