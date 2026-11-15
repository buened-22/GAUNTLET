"""Actor subprocess: hosts one implementation instance behind statement gates.

The actor process has NO database connection, no DSN and no credentials (audit finding A-01). The harness
process owns the actor's connection; the actor asks it to execute each statement and receives the result.

Protocol (JSON lines; parent -> child on stdin, child -> parent on stdout):

    parent -> {"type": "init", "impl", "name", "sandbox"?}
    child  -> {"type": "hello", "os_pid": int}
    parent -> {"type": "op", "op_id": str, "method": str, "args": {...}}
    child  -> {"type": "gate", "op_id", "seq", "sql", "params"}          (params: gauntlet.actor.wire encoding)
    parent -> {"type": "result", "seq", "rows", "rowcount", "error", "exception", "in_txn"}
              (sent once the controller granted the statement and the harness executed it)
    ...
    child  -> {"type": "respond", "op_id", "outcome": {...}}
    parent -> {"type": "grant"}          (response delivered; or the parent kills us)
    parent -> {"type": "exit"}

Every database statement (including BEGIN/COMMIT/ROLLBACK) is executed by the harness only after the
controller granted it, so the parent fully controls and records the interleaving of statements across
actors. The statement class and the transaction status are determined by the harness, never reported by the
actor. Implementation code runs only here, never in the harness or API process.
"""

from __future__ import annotations

import json
import os
import re
import sys
import traceback
from contextlib import contextmanager
from decimal import Decimal
from typing import Any

from gauntlet.actor import wire
from gauntlet.impls.base import AppError


# Leading whitespace and SQL comments are not part of the statement's verb.
_LEADING = re.compile(r"\A(?:\s+|--[^\n]*(?:\n|\Z)|/\*.*?\*/)*", re.S)
_WRITE_WORD = re.compile(r"\b(?:INSERT|UPDATE|DELETE|MERGE)\b")


def classify(sql: str) -> str:
    """Statement class used for fault points (``before_write``) and the trace (computed by the harness).

    A misclassified write would silently skip a ``before_write`` fault, so unknown statements that can modify
    data (MERGE, CALL, DO, COPY) count as writes. Every statement recorded in the frozen evidence keeps its
    class (``tests/test_audit_hardening.py``)."""
    body = _LEADING.sub("", sql, count=1)
    m = re.match(r"[A-Za-z]+", body)
    head = m.group(0).upper() if m else ""
    if head == "SELECT":
        return "read"
    if head == "WITH":
        return "write" if _WRITE_WORD.search(body.upper()) else "read"
    if head in ("INSERT", "UPDATE", "DELETE", "MERGE", "CALL", "DO", "COPY"):
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


class DbError(Exception):
    """A database error reported by the harness for a statement this actor issued."""

    def __init__(self, sqlstate: str | None, message: str):
        super().__init__(message)
        self.sqlstate = sqlstate
        self.message = message


class StatementException(Exception):
    """A non-database error raised while the harness executed the statement (e.g. unadaptable parameters)."""

    def __init__(self, type_name: str, message: str):
        super().__init__(message)
        self.type_name = type_name


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


class _Result:
    def __init__(self, rows: list[dict] | None, rowcount: int | None):
        self.rows = rows
        self.rowcount = rowcount


class GatedDb:
    """The ``Db`` handed to implementations: a request channel to the harness, which owns the connection."""

    def __init__(self, chan: Channel):
        self._chan = chan
        self.op_id: str | None = None
        self._seq = 0
        self._in_txn_flag = False  # as reported by the harness after the last statement

    def _in_txn(self) -> bool:
        return self._in_txn_flag

    def _run(self, sql: str, params: Any = None) -> _Result:
        if not isinstance(sql, str):
            raise TypeError("sql must be a string")
        self._seq += 1
        seq = self._seq
        self._chan.send({"type": "gate", "op_id": self.op_id, "seq": seq, "sql": sql,
                         "params": wire.encode(params)})
        msg = self._chan.recv()
        if msg.get("type") != "result" or msg.get("seq") != seq:
            raise RuntimeError(f"protocol violation: expected the result of statement {seq}, got {msg}")
        self._in_txn_flag = bool(msg.get("in_txn"))
        if msg.get("error") is not None:
            raise DbError(msg["error"].get("sqlstate"), msg["error"].get("message", ""))
        if msg.get("exception") is not None:
            raise StatementException(msg["exception"].get("type", "Exception"), msg["exception"].get("message", ""))
        rows = msg.get("rows")
        return _Result(wire.decode(rows) if rows is not None else None, msg.get("rowcount"))

    def query(self, sql: str, params: Any = None) -> list[dict]:
        res = self._run(sql, params)
        return res.rows if res.rows is not None else []

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
            if self._in_txn():
                try:
                    self._run("ROLLBACK")
                except DbError:
                    pass
            raise
        else:
            self._run("COMMIT")

    def ensure_idle(self) -> None:
        """Roll back a transaction an implementation left open after an error."""
        if self._in_txn():
            self._run("ROLLBACK")


def main() -> None:
    # Processes may be pre-started (warm pool); identity arrives in an init message.
    from gauntlet.impls.registry import load_implementation

    chan = Channel()
    init = chan.recv()
    if init.get("type") != "init":
        chan.send({"type": "fatal", "error": f"expected init, got {init}"})
        return
    try:
        impl = load_implementation(init["impl"], init.get("sandbox"))
    except Exception as exc:  # e.g. a candidate that fails to import: report, never run
        chan.send({"type": "fatal", "error": f"could not load implementation: {type(exc).__name__}: {exc}"})
        return
    chan.send({"type": "hello", "os_pid": os.getpid()})
    db = GatedDb(chan)

    while True:
        msg = chan.recv()
        if msg["type"] == "exit":
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
            except DbError as exc:
                outcome = {"status": "error", "error": {"kind": "db", "code": "db_error",
                                                         "sqlstate": exc.sqlstate, "message": exc.message}}
            except StatementException as exc:
                outcome = {"status": "error", "error": {"kind": "exception", "code": exc.type_name,
                                                         "message": str(exc),
                                                         "traceback": traceback.format_exc(limit=5)}}
            except Exception as exc:  # implementation bug (not harness): still a client-visible error
                outcome = {"status": "error", "error": {"kind": "exception", "code": type(exc).__name__,
                                                         "message": str(exc),
                                                         "traceback": traceback.format_exc(limit=5)}}
            try:
                db.ensure_idle()
            except DbError:
                pass
        chan.send({"type": "respond", "op_id": db.op_id, "outcome": outcome})
        grant = chan.recv()
        if grant.get("type") == "exit":
            return
        db.op_id = None


if __name__ == "__main__":
    main()
