"""The harness-side database connection of one actor (audit finding A-01).

Implementation code runs in the actor subprocess, which has no database connection, no DSN and no
credentials. The harness process owns each actor's connection: a statement the controller granted is
executed here, on a short-lived thread so that a statement waiting for a lock never blocks the
single-threaded controller, and its result is sent back to the actor. Every statement that reaches the
database therefore went through a gate and is recorded in the trace.

Each granted request is exactly ONE SQL statement: statements are sent with the extended query protocol, so
the server rejects a string containing several statements before executing any of it (otherwise a request
without parameters would use the simple protocol, which runs ``SELECT 1; UPDATE ...`` under one gate and one
statement class). Results are fetched row by row and bounded (``MAX_RESULT_ROWS`` / ``MAX_RESULT_BYTES``), so
a statement cannot make the harness buffer an arbitrarily large result: past the budget the statement is
cancelled and the request fails.

The statement is sent and its results are read here, through psycopg's documented low-level interfaces only:
``psycopg.pq`` (the libpq wrapper: ``send_query_params``, single-row mode, ``get_result``) and
``psycopg.adapt.Transformer`` (the same parameter and result adaptation psycopg's cursors use). No psycopg
cursor is involved and nothing private to psycopg is used or overridden. psycopg's own ``Cursor.stream()`` is
not usable for this because it refuses statements that return no rows, and ``Cursor.execute()`` buffers the
whole result. ``tests/test_audit_hardening_pg.py`` checks the outcome against ``execute()`` statement by
statement.

A crashed actor's process no longer takes its database session with it, so ``close`` emulates that: an idle
connection is closed (the backend exits and rolls back any open transaction); a connection with a statement
still running is terminated by the caller-supplied ``terminate`` (pg_terminate_backend on the harness's
monitor connection), exactly as the operating system did when the old in-process connection died.
"""

from __future__ import annotations

import json
import select
import threading
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Callable, Iterator

import psycopg
from psycopg import pq
from psycopg.adapt import PyFormat, Transformer
from psycopg.pq import DiagnosticField, ExecStatus

from gauntlet import config
from gauntlet.actor import wire


@dataclass
class StatementResult:
    rows: list | None             # result rows, already wire-encoded; None when the statement returns no rows
    rowcount: int | None          # as psycopg's Cursor.rowcount after execute()
    error: dict | None            # {"sqlstate", "message"} for a database error
    exception: dict | None        # {"type", "message"} for a non-database error raised while executing
    in_txn: bool                  # transaction status after the statement
    failure: str | None = None    # the harness refused the result (over budget, or not representable)


_FORMATS = {"s": PyFormat.AUTO, "b": PyFormat.BINARY, "t": PyFormat.TEXT}
_COPY = (ExecStatus.COPY_IN, ExecStatus.COPY_OUT, ExecStatus.COPY_BOTH)


def bind(sql: str, params: Any) -> tuple[str, list | None, list | None]:
    """Rewrite psycopg-style placeholders to PostgreSQL's ``$n`` and put the parameters in that order.

    Returns ``(query, values, formats)``; ``values`` and ``formats`` are None when ``params`` is None, in which
    case the text is sent unchanged (a ``%`` is then just a character). Otherwise ``%s``, ``%b`` and ``%t`` are
    positional placeholders taking a sequence, ``%(name)s`` (``b``, ``t``) are named placeholders taking a
    mapping, where a repeated name is one parameter, and ``%%`` is a literal percent sign. The two kinds cannot
    be mixed. As in psycopg, the text is not parsed as SQL, so a placeholder inside a string literal counts.

    Raises TypeError when ``params`` has the wrong shape and psycopg.ProgrammingError when the placeholders
    and the parameters do not fit: the same exception types psycopg's cursors raise for these mistakes.
    """
    if params is None:
        return sql, None, None
    if isinstance(params, (str, bytes)) or not isinstance(params, (Sequence, Mapping)):
        raise TypeError(f"query parameters must be a sequence or a mapping, not {type(params).__name__}")
    out: list[str] = []
    names: list[str | None] = []        # one entry per placeholder occurrence (None for a positional one)
    formats: list[PyFormat] = []
    slots: dict[str, int] = {}          # named placeholder -> its $n
    numbered = 0
    i, n = 0, len(sql)
    while i < n:
        j = sql.find("%", i)
        if j < 0:
            out.append(sql[i:])
            break
        out.append(sql[i:j])
        if j + 1 >= n:                  # a lone % at the very end: left as it is
            out.append("%")
            break
        nxt = sql[j + 1]
        if nxt == "%":
            out.append("%")
            i = j + 2
            continue
        name = None
        k = j + 1
        if nxt == "(":
            close = sql.find(")", j + 2)
            if close < 0 or close + 1 >= n:
                raise psycopg.ProgrammingError(f"unfinished placeholder {sql[j:j + 20]!r}")
            name, k = sql[j + 2:close], close + 1
        fmt = _FORMATS.get(sql[k])
        if fmt is None:
            raise psycopg.ProgrammingError(f"{sql[j:k + 1]!r} is not a placeholder: only %s, %b and %t are, "
                                           f"and a literal percent sign is written %%")
        if names and (names[-1] is None) != (name is None):
            raise psycopg.ProgrammingError("positional and named placeholders cannot be used in the same statement")
        if name is None:
            numbered += 1
            out.append(f"${numbered}")
            formats.append(fmt)
        elif name in slots:
            if formats[slots[name] - 1] != fmt:
                raise psycopg.ProgrammingError(f"placeholder {name!r} is used with two different formats")
            out.append(f"${slots[name]}")
        else:
            numbered += 1
            slots[name] = numbered
            out.append(f"${numbered}")
            formats.append(fmt)
        names.append(name)
        i = k + 1
    if slots:
        if not isinstance(params, Mapping):
            raise TypeError("named placeholders need a mapping of parameters")
        missing = [name for name in slots if name not in params]
        if missing:
            raise psycopg.ProgrammingError(f"no parameter was given for the placeholder {missing[0]!r}")
        values = [params[name] for name in slots]
    elif isinstance(params, Mapping):
        if numbered:
            raise TypeError("positional placeholders need a sequence of parameters")
        values = []
    else:
        if len(params) != numbered:
            raise psycopg.ProgrammingError(f"the statement has {numbered} placeholder(s) but {len(params)} "
                                           f"parameter(s) were given")
        values = list(params)
    return "".join(out), values, formats


class ActorConnection:
    def __init__(self, dsn: str, name: str, lock_timeout_ms: int):
        self.name = name
        self._conn = psycopg.connect(dsn, autocommit=True, application_name=f"gauntlet:{name}",
                                     connect_timeout=config.ACTOR_CONNECT_TIMEOUT_S)
        try:
            self._conn.execute(f"SET lock_timeout = {int(lock_timeout_ms)}")
        except BaseException:
            self._conn.close()
            raise
        self.backend_pid: int = self._conn.info.backend_pid
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self._result: StatementResult | None = None
        self.closed = False

    # -- state (read only while no statement is running) --------------------------------------------------
    def in_txn(self) -> bool:
        return (not self._conn.closed
                and self._conn.info.transaction_status != psycopg.pq.TransactionStatus.IDLE)

    @property
    def busy(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    # -- statements -------------------------------------------------------------------------------------
    def start(self, sql: str, params: Any) -> None:
        """Begin executing one granted statement on a background thread."""
        with self._lock:
            if self.closed:
                raise RuntimeError(f"connection of {self.name} is closed")
            if self.busy:
                raise RuntimeError(f"connection of {self.name} is already executing a statement")
            self._result = None
            self._thread = threading.Thread(target=self._execute, args=(sql, params),
                                            name=f"gauntlet-stmt-{self.name}", daemon=True)
            self._thread.start()

    def _execute(self, sql: str, params: Any) -> None:
        max_rows, max_bytes = config.MAX_RESULT_ROWS, config.MAX_RESULT_BYTES
        rows: list = []
        size = 0
        failure = None                  # the harness refused the result (budget, or a value it cannot send)
        error = None                    # the statement failed
        final = None                    # (status, command row count) of the result that ends the statement
        columns: list[str] = []
        sent = finished = False
        try:
            query, values, formats = bind(sql, params)
            adapt = Transformer.from_context(self._conn)
            encoding = self._conn.info.encoding
            pgconn = self._conn.pgconn
            if values is None:
                pgconn.send_query_params(query.encode(encoding), None)
            else:
                dumped = adapt.dump_sequence(values, formats)
                pgconn.send_query_params(query.encode(encoding), dumped, param_types=adapt.types,
                                         param_formats=adapt.formats)
            sent = True
            pgconn.set_single_row_mode()   # one result per row, so that the budgets below can stop a large one
            for res in self._results():
                status = res.status
                if status == ExecStatus.SINGLE_TUPLE:
                    if failure is not None or error is not None:
                        continue           # rows still arriving after the statement was stopped: discarded
                    if len(rows) >= max_rows:
                        failure = f"the result has more than {max_rows} rows (harness limit MAX_RESULT_ROWS)"
                        self.cancel()
                        continue
                    if not columns:
                        adapt.set_pgresult(res)
                        columns = [res.fname(c).decode(encoding) for c in range(res.nfields)]
                    else:
                        adapt.set_pgresult(res, set_loaders=False)
                    row = adapt.load_row(0, lambda vals: dict(zip(columns, vals)))
                    try:
                        enc = wire.encode(row)
                        size += len(json.dumps(enc)) + 1
                    except (wire.WireError, RecursionError, ValueError) as exc:
                        failure = f"a result value cannot be sent to the actor ({exc})"
                        self.cancel()
                        continue
                    if size > max_bytes:
                        failure = f"the result is larger than {max_bytes} bytes (harness limit MAX_RESULT_BYTES)"
                        self.cancel()
                        continue
                    rows.append(enc)
                elif status in (ExecStatus.TUPLES_OK, ExecStatus.COMMAND_OK):
                    final = (status, res.command_tuples)
                elif status == ExecStatus.FATAL_ERROR:
                    if error is None:
                        state = res.error_field(DiagnosticField.SQLSTATE)
                        text = pq.error_message(res, encoding).strip()
                        error = {"sqlstate": state.decode("ascii") if state else None,
                                 "message": text.splitlines()[0] if text else ""}
                elif status in _COPY:
                    if error is None:
                        error = {"sqlstate": None, "message": "COPY to or from the client is not supported"}
                    self._abandon_copy(status)
                elif status == ExecStatus.EMPTY_QUERY:
                    if error is None:
                        error = {"sqlstate": None, "message": "the statement is empty"}
                elif error is None:
                    error = {"sqlstate": None,
                             "message": f"unexpected result status {ExecStatus(status).name} from the statement"}
            finished = True
            if failure is not None:
                res_ = StatementResult(None, None, None, None, self.in_txn(), failure=failure)
            elif error is not None:
                res_ = StatementResult(None, None, error, None, self.in_txn())
            elif final is not None and final[0] == ExecStatus.TUPLES_OK:
                res_ = StatementResult(rows, len(rows), None, None, self.in_txn())
            else:  # a statement without result rows: its command row count, or -1 when it has none
                n = final[1] if final is not None else None
                res_ = StatementResult(None, n if n is not None else -1, None, None, self.in_txn())
        except psycopg.Error as exc:
            text = str(exc).strip()
            res_ = StatementResult(None, None, {"sqlstate": exc.sqlstate,
                                                "message": text.splitlines()[0] if text else ""},
                                   None, self.in_txn())
        except Exception as exc:  # noqa: BLE001 - reported to the actor like any other statement failure
            res_ = StatementResult(None, None, None, {"type": type(exc).__name__, "message": str(exc)},
                                   self.in_txn())
        finally:
            if sent and not finished:      # never leave a half-read statement on the connection
                self.cancel()
                try:
                    for _ in self._results():
                        pass
                except Exception:  # noqa: BLE001 - the connection is unusable; close() deals with it
                    pass
        self._result = res_

    def _results(self) -> Iterator[pq.PGresult]:
        """Every result libpq produces for the statement that was just sent, until it reports the end. The
        connection is non-blocking: wait for the socket, let libpq read, and take a result when one is ready."""
        pgconn = self._conn.pgconn
        fd = pgconn.socket
        while pgconn.flush():              # 1: part of the request is still in libpq's output buffer
            readable, _w, _x = select.select([fd], [fd], [], 1.0)
            if readable:
                pgconn.consume_input()
        while True:
            while pgconn.is_busy():
                select.select([fd], [], [], 1.0)
                pgconn.consume_input()
            res = pgconn.get_result()
            if res is None:
                return
            yield res

    def _abandon_copy(self, status: ExecStatus) -> None:
        """Leave a COPY the statement started: refuse to supply data, and discard data the server sends."""
        pgconn = self._conn.pgconn
        if status in (ExecStatus.COPY_IN, ExecStatus.COPY_BOTH):
            pgconn.put_copy_end(b"COPY from the client is not supported")
        if status in (ExecStatus.COPY_OUT, ExecStatus.COPY_BOTH):
            self.cancel()
            fd = pgconn.socket
            while True:
                n, _data = pgconn.get_copy_data(1)
                if n == 0:                 # nothing buffered yet
                    select.select([fd], [], [], 1.0)
                    pgconn.consume_input()
                elif n < 0:                # -1: the copy has ended (a result follows); -2: an error
                    return

    def wait(self, timeout: float) -> StatementResult | None:
        """The finished statement's result, or None if it is still running after ``timeout`` seconds."""
        t = self._thread
        if t is None:
            raise RuntimeError(f"no statement was started on the connection of {self.name}")
        t.join(timeout)
        if t.is_alive():
            return None
        self._thread = None
        return self._result

    # -- shutdown ---------------------------------------------------------------------------------------
    def cancel(self) -> None:
        """Thread-safe: cancel a running statement (used by the watchdog thread)."""
        try:
            if not self._conn.closed:
                self._conn.cancel_safe()
        except Exception:  # noqa: BLE001
            pass

    def close(self, terminate: Callable[[int], None] | None = None, timeout: float = 10) -> bool:
        """End the session as a process crash would. Idempotent. Returns False only if a running statement
        could not be stopped: the connection is then left open rather than freed under a running thread."""
        with self._lock:
            if self.closed:
                return True
            self.closed = True
        if self.busy:
            try:
                if terminate is not None:
                    terminate(self.backend_pid)
                else:
                    self.cancel()
            except Exception:  # noqa: BLE001 - fall back to a cancel request
                self.cancel()
            t = self._thread
            if t is not None:
                t.join(timeout)
                if t.is_alive():
                    return False
        try:
            self._conn.close()
        except Exception:  # noqa: BLE001
            pass
        return True
