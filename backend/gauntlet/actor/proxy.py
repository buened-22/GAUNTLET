"""Parent-side handle for an actor subprocess.

The proxy tracks the actor's state machine::

    IDLE --submit--> AT_GATE | AT_RESPOND
    AT_GATE --grant--> AT_GATE | AT_RESPOND | BLOCKED
    BLOCKED --(lock released)--> AT_GATE | AT_RESPOND
    AT_RESPOND --deliver--> IDLE
    any --kill--> DEAD

Lock waits are detected logically with ``pg_blocking_pids()`` on the harness's
monitor connection rather than by timeouts, so whether a statement is blocked
is a property of database state, not of wall-clock timing.
"""

from __future__ import annotations

import json
import os
import queue
import reprlib
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Callable

from gauntlet.actor import wire
from gauntlet.actor.child import _jsonable, classify
from gauntlet.actor.connection import ActorConnection, StatementResult
from gauntlet.config import CHILD_LOCK_TIMEOUT_MS, STEP_CANCEL_GRACE_S, STEP_STALL_GAP_S, STEP_TIMEOUT_S
from gauntlet.pg.provision import InfrastructureError

BACKEND_DIR = Path(__file__).resolve().parents[2]


class ActorFailure(InfrastructureError):
    """The actor process died, broke the protocol or stopped responding.

    ``loaded`` is True once the actor reported that the implementation was loaded (hello received), or when
    loading the implementation itself failed; only then can the failure be attributed to the implementation.
    """

    def __init__(self, message: str, loaded: bool):
        super().__init__(message)
        self.loaded = loaded


class StepTimeout(InfrastructureError):
    """A granted statement neither completed nor blocked on a lock within ``STEP_TIMEOUT_S`` (audit finding FR-3).

    The statement has been cancelled (or its session terminated) and the actor stopped before this is raised.
    ``evidence`` records what the harness observed; ``attributable`` is True only when that evidence shows the
    harness and the server were healthy and the statement itself was the slow part (see
    :func:`step_timeout_attribution`). The runner classifies an attributable timeout of a loaded *external*
    candidate as ``candidate_statement_timeout``; every other step timeout stays ``infrastructure``.
    """

    def __init__(self, message: str, *, evidence: dict, attributable: bool, reason: str, loaded: bool):
        super().__init__(message)
        self.evidence = evidence
        self.attributable = attributable
        self.reason = reason
        self.loaded = loaded


def step_timeout_attribution(evidence: dict, step_timeout_s: float, stall_gap_s: float) -> tuple[bool, str]:
    """Decide whether a step timeout can be attributed to the statement itself. Every condition must hold;
    anything missing or unusual leaves it unattributed (an infrastructure error), never a guess.

    1. The harness's own wait loop never stalled for longer than ``stall_gap_s`` (a host sleep, heavy load or a
       slow monitor query shows up as a long gap between iterations).
    2. The server answered a query about the statement's backend, and reported it ``active``, executing this
       statement for (almost) the whole step, either on CPU (no wait event) or in ``pg_sleep``. Waits on I/O,
       lightweight locks, the client or anything else can equally be caused by a slow host or server.
    3. A cancel request then ended the statement with SQLSTATE 57014 (or it completed meanwhile) within the
       grace period. 57014 alone proves only that the server processed a cancel, so it is never sufficient.
    """
    gap = evidence.get("max_loop_gap_s")
    if gap is None or gap > stall_gap_s:
        return False, (f"the harness's wait loop stalled for {gap}s (more than {stall_gap_s}s), so a host or "
                       f"harness stall cannot be excluded")
    act = evidence.get("activity")
    if not isinstance(act, dict):
        return False, "the server's view of the statement could not be read"
    if act.get("state") != "active":
        return False, f"the backend was not executing a statement (state {act.get('state')!r})"
    elapsed = act.get("elapsed_s")
    if not isinstance(elapsed, (int, float)) or elapsed < 0.9 * step_timeout_s:
        return False, f"the server had not been executing the statement for the whole step (elapsed {elapsed}s)"
    wait = (act.get("wait_event_type"), act.get("wait_event"))
    if wait not in ((None, None), ("Timeout", "PgSleep")):
        return False, (f"the backend was waiting on {wait[0]}/{wait[1]}, which a slow host or server can also "
                       f"cause")
    cancel = evidence.get("cancel")
    if cancel not in ("cancelled", "completed"):
        return False, f"the cancel request did not end the statement cleanly ({cancel})"
    return True, ("the server was executing the statement for the whole step, the harness did not stall, and "
                  "the statement ended on a cancel request")


class ActorState(str, Enum):
    IDLE = "idle"
    AT_GATE = "at_gate"
    BLOCKED = "blocked"
    AT_RESPOND = "at_respond"
    DEAD = "dead"


@dataclass
class Gate:
    op_id: str
    seq: int
    sql: str            # whitespace-normalized SQL, as recorded in the trace
    params: Any         # JSON rendering of the parameters, as recorded in the trace
    cls: str            # statement class, determined by the harness
    in_txn: bool        # transaction status before the statement, observed by the harness
    raw_sql: str = ""   # the statement as issued, executed by the harness
    exec_params: Any = None  # decoded parameters, executed by the harness


@dataclass
class OpProgress:
    op_id: str
    statements: int = 0
    commits: int = 0
    writes: int = 0
    outcome: dict | None = None
    stmt_log: list[dict] = field(default_factory=list)


MAX_PROTOCOL_LINE = 4 * 1024 * 1024  # sandboxed actors: longer stdout lines are a protocol violation
# Renders untrusted protocol messages in error text with bounded depth and size (str() of a deeply nested
# message would itself raise RecursionError).
_SHOW = reprlib.Repr()
# set as attributes: the keyword arguments of Repr() only exist from Python 3.12
_SHOW.maxlevel, _SHOW.maxstring, _SHOW.maxother, _SHOW.maxlist, _SHOW.maxdict = 4, 200, 200, 8, 8


class ChildProcess:
    """A started (possibly not yet initialised) actor subprocess with reader threads.

    With ``sandbox`` (an ``IsolationConfig``, used for external candidates) the process is started by
    ``gauntlet.candidates.sandbox.ActorSandbox``: allow-listed environment, own workspace, job object /
    process group, resource limits, bounded output capture. Without it, behaviour is unchanged.
    """

    def __init__(self, sandbox=None) -> None:
        creationflags = subprocess.CREATE_NO_WINDOW if sys.platform.startswith("win") else 0
        self.sandbox = None
        self.output_limit = None
        self.stderr_bytes = 0
        self.stderr_truncated = False
        if sandbox is not None:
            from gauntlet.candidates.sandbox import ActorSandbox
            self.sandbox = ActorSandbox(sandbox, pythonpath=str(BACKEND_DIR))
            self.output_limit = sandbox.output_limit_bytes
            try:
                self.proc = self.sandbox.popen(
                    [self.sandbox.interpreter, "-s", "-m", "gauntlet.actor.child"],
                    stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                    text=True, encoding="utf-8", errors="replace", bufsize=1, creationflags=creationflags)
            except BaseException:
                self.sandbox.cleanup()
                raise
        else:
            env = dict(os.environ)
            env["PYTHONPATH"] = str(BACKEND_DIR) + os.pathsep + env.get("PYTHONPATH", "")
            env["PYTHONUNBUFFERED"] = "1"
            self.proc = subprocess.Popen(
                [sys.executable, "-m", "gauntlet.actor.child"],
                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                text=True, encoding="utf-8", bufsize=1, env=env, creationflags=creationflags,
            )
        self.q: queue.Queue = queue.Queue()
        self.stderr_tail: list[str] = []
        threading.Thread(target=self._reader, daemon=True).start()
        threading.Thread(target=self._err_reader, daemon=True).start()

    def _reader(self) -> None:
        assert self.proc.stdout is not None
        if self.sandbox is None:
            for line in self.proc.stdout:
                line = line.strip()
                if line:
                    self.q.put(self._parse(line))
            self.q.put({"type": "eof"})
            return
        while True:  # bounded reads: an over-long line is a protocol violation, never unbounded memory
            line = self.proc.stdout.readline(MAX_PROTOCOL_LINE)
            if not line:
                break
            if len(line) >= MAX_PROTOCOL_LINE and not line.endswith("\n"):
                self.q.put({"type": "garbage", "line": f"protocol line longer than {MAX_PROTOCOL_LINE} characters"})
                while self.proc.stdout.read(65536):
                    pass
                break
            line = line.strip()
            if line:
                self.q.put(self._parse(line))
        self.q.put({"type": "eof"})

    @staticmethod
    def _parse(line: str) -> dict:
        """One protocol line as a message. A message is a JSON object: a line that is not valid JSON, that is
        nested too deeply to parse, or that is valid JSON of another type (``[1]``, ``123``, ``null``) is
        garbage, which ends the actor as a protocol violation. Whether a very deep line raises RecursionError
        or parses into a nested list depends on the platform's stack; both end here."""
        try:
            msg = json.loads(line)
        except (json.JSONDecodeError, RecursionError):
            msg = None
        return msg if isinstance(msg, dict) else {"type": "garbage", "line": line[:500]}

    def _err_reader(self) -> None:
        assert self.proc.stderr is not None
        while True:
            line = self.proc.stderr.readline(65536) if self.sandbox else self.proc.stderr.readline()
            if not line:
                return
            self.stderr_bytes += len(line)
            if self.output_limit is not None and self.stderr_bytes > self.output_limit:
                if not self.stderr_truncated:
                    self.stderr_truncated = True
                    self.stderr_tail.append(f"[output limit of {self.output_limit} bytes reached; further output "
                                            "is drained and counted, not stored]")
                continue
            self.stderr_tail.append(line.rstrip()[:2000])
            del self.stderr_tail[:-30]

    def kill(self) -> None:
        if self.sandbox is not None:
            self.sandbox.kill_tree(self.proc)  # the whole job / process group, not just the actor
            self.sandbox.cleanup()
            return
        if self.proc.poll() is None:
            self.proc.kill()
            self.proc.wait(timeout=10)

    def release(self) -> None:
        """After the process has exited: remove its sandbox workspace and job."""
        if self.sandbox is not None:
            if self.proc.poll() is None:
                self.sandbox.kill_tree(self.proc)
            self.sandbox.cleanup()


class ProcessPool:
    """Pre-started actor processes, so that Python start-up cost overlaps.

    Each actor incarnation (spawn or restart) still gets its own fresh OS
    process; the pool only starts them ahead of time.
    """

    def __init__(self, size: int = 0, sandbox=None):
        self.sandbox = sandbox
        self._free: list[ChildProcess] = []
        try:
            for _ in range(size):
                self._free.append(ChildProcess(sandbox))
        except BaseException:
            self.close()
            raise

    def take(self) -> ChildProcess:
        return self._free.pop(0) if self._free else ChildProcess(self.sandbox)

    def close(self) -> None:
        """Kill every pre-started process. One failing kill never prevents the others; the first error is
        re-raised afterwards so the caller can record it. Calling close() again is a no-op."""
        free, self._free = self._free, []
        first: BaseException | None = None
        for c in free:
            try:
                c.kill()
            except Exception as exc:  # noqa: BLE001 - keep cleaning up
                first = first or exc
        if first is not None:
            raise first


class ActorProxy:
    """Parent-side handle for one actor. It owns the actor's database connection (audit finding A-01): the
    actor process only *requests* statements; each one is executed here after the controller granted it."""

    def __init__(self, name: str, role: str, impl_id: str, dsn: str,
                 blocking_probe: Callable[[int], list[int]], pool: ProcessPool | None = None,
                 terminate_backend: Callable[[int], None] | None = None,
                 on_execute: Callable[[str, Gate], None] | None = None,
                 activity_probe: Callable[[int], dict | None] | None = None):
        self.name = name
        self.role = role  # "v1" | "v2" | "migrator"
        self.impl_id = impl_id
        self.dsn = dsn
        self._probe = blocking_probe
        self._terminate = terminate_backend
        self._activity = activity_probe  # the server's view of a backend (evidence for a step timeout only)
        self._on_execute = on_execute  # records every statement executed on this actor's connection
        self._pool = pool or ProcessPool()
        self.state = ActorState.DEAD
        self.child: ChildProcess | None = None
        self.conn: ActorConnection | None = None
        self.backend_pid: int | None = None
        self.gate: Gate | None = None
        self.progress: OpProgress | None = None
        self.incarnation = 0
        self._last_seq = 0  # highest statement seq seen from the current incarnation
        self._loaded = False  # hello received from the current incarnation
        self.cleanup_errors: list[str] = []  # connections that could not be shut down (collected by the controller)

    @property
    def proc(self) -> subprocess.Popen | None:
        return self.child.proc if self.child else None

    @property
    def _q(self) -> queue.Queue:
        assert self.child is not None
        return self.child.q

    # -- process lifecycle -------------------------------------------------
    def spawn(self) -> None:
        self.child = self._pool.take()
        self.incarnation += 1
        self._last_seq = 0
        self._loaded = False
        self.backend_pid = None
        try:
            self.conn = ActorConnection(self.dsn, self.name, CHILD_LOCK_TIMEOUT_MS)
        except Exception as exc:  # noqa: BLE001 - the database is unavailable: an infrastructure error
            self.child.kill()
            raise InfrastructureError(f"actor {self.name}: could not open its database connection: {exc}") from exc
        init = {"type": "init", "impl": self.impl_id, "name": self.name}  # no DSN: the actor has no database access
        if self.child.sandbox is not None:
            from gauntlet.candidates.snapshots import snapshots_root
            init["sandbox"] = {"workspace": str(self.child.sandbox.workspace), "snapshots_root": str(snapshots_root())}
        self._send(init)

    def wait_hello(self) -> None:
        msg = self._recv(timeout=STEP_TIMEOUT_S * 3)
        if msg.get("type") != "hello":
            raise InfrastructureError(f"actor {self.name}: bad hello {msg}")
        assert self.conn is not None
        self._loaded = True
        self.backend_pid = self.conn.backend_pid
        self.state = ActorState.IDLE

    def _send(self, msg: dict) -> None:
        assert self.proc is not None and self.proc.stdin is not None
        try:
            self.proc.stdin.write(json.dumps(msg) + "\n")
            self.proc.stdin.flush()
        except OSError as exc:
            raise ActorFailure(f"actor {self.name}: pipe closed ({exc}); stderr={self.stderr_tail()}",
                               loaded=self._loaded) from exc

    def _recv(self, timeout: float) -> dict:
        try:
            msg = self._q.get(timeout=timeout)
        except queue.Empty:
            raise ActorFailure(f"actor {self.name}: no message within {timeout}s; stderr={self.stderr_tail()}",
                               loaded=self._loaded) from None
        if msg.get("type") in ("eof", "garbage", "fatal"):
            load_failed = msg.get("type") == "fatal" and "could not load implementation" in str(msg.get("error"))
            raise ActorFailure(f"actor {self.name}: died or broke protocol: {msg}; stderr={self.stderr_tail()}",
                               loaded=self._loaded or load_failed)
        return msg

    def stderr_tail(self) -> str:
        return " | ".join(self.child.stderr_tail[-8:]) if self.child else ""

    def _close_conn(self) -> None:
        if self.conn is not None and not self.conn.close(terminate=self._terminate):
            self.cleanup_errors.append(f"actor {self.name} (incarnation {self.incarnation}): a running statement "
                                       f"could not be stopped; its database connection was left open")

    def kill(self) -> None:
        """Hard-kill the process (TerminateProcess / SIGKILL) and end its database session, as the death of the
        process ended it when the actor owned its connection (an open transaction is rolled back)."""
        try:
            if self.child:
                self.child.kill()
        finally:
            self._close_conn()
            self.state = ActorState.DEAD
            self.gate = None

    def abort(self) -> None:
        """From the watchdog thread: kill the process and cancel a running statement (thread-safe). The
        connection itself is closed later by the controller thread."""
        if self.child:
            self.child.kill()
        if self.conn is not None:
            self.conn.cancel()

    def exit(self) -> None:
        try:
            if self.proc and self.proc.poll() is None:
                try:
                    self._send({"type": "exit"})
                    self.proc.wait(timeout=10)
                except (InfrastructureError, subprocess.TimeoutExpired):
                    self.proc.kill()
            if self.child is not None:
                self.child.release()
        finally:
            self._close_conn()
            self.state = ActorState.DEAD

    # -- operation control --------------------------------------------------
    def submit(self, op_id: str, method: str, args: dict) -> None:
        if self.state != ActorState.IDLE:
            raise InfrastructureError(f"actor {self.name} not idle (state={self.state})")
        self.progress = OpProgress(op_id=op_id)
        self._send({"type": "op", "op_id": op_id, "method": method, "args": args})
        self._await_next()

    def _violation(self, why: str, msg: Any) -> ActorFailure:
        """A message that does not fit the protocol state: the actor is not trusted to continue. Never a pass:
        for an external candidate it is a candidate execution failure, otherwise an infrastructure error."""
        return ActorFailure(f"actor {self.name}: protocol violation: {why}: {_SHOW.repr(msg)}; "
                            f"stderr={self.stderr_tail()}", loaded=self._loaded)

    def _await_next(self) -> None:
        msg = self._recv(timeout=STEP_TIMEOUT_S)
        op_id = self.progress.op_id if self.progress else None
        kind = msg.get("type") if isinstance(msg, dict) else None
        if kind == "gate":
            seq, sql = msg.get("seq"), msg.get("sql")
            if msg.get("op_id") != op_id:
                raise self._violation(f"gate for operation {msg.get('op_id')!r} while {op_id!r} is pending", msg)
            if type(seq) is not int or seq <= self._last_seq:
                raise self._violation(f"statement seq {seq!r} is not greater than {self._last_seq}", msg)
            if not isinstance(sql, str) or not sql.strip() or "params" not in msg:
                raise self._violation("malformed gate message", msg)
            try:
                params = wire.decode_params(msg["params"])
            except (wire.WireError, RecursionError) as exc:
                raise self._violation(f"malformed statement parameters ({exc})", msg) from None
            assert self.conn is not None
            self._last_seq = seq
            # The statement class and the transaction status are the harness's own observations.
            self.gate = Gate(op_id, seq, " ".join(sql.split()), _jsonable(params), classify(sql),
                             self.conn.in_txn(), raw_sql=sql, exec_params=params)
            self.state = ActorState.AT_GATE
        elif kind == "respond":
            outcome = msg.get("outcome")
            if msg.get("op_id") != op_id:
                raise self._violation(f"response for operation {msg.get('op_id')!r} while {op_id!r} is pending", msg)
            if not isinstance(outcome, dict) or outcome.get("status") not in ("ok", "error"):
                raise self._violation("malformed response outcome", msg)
            assert self.progress is not None
            self.progress.outcome = outcome
            self.gate = None
            self.state = ActorState.AT_RESPOND
        else:
            raise self._violation("expected a gate or a response", msg)

    def grant(self) -> dict:
        """Execute the pending statement on the actor's connection. Returns a stmt record.

        If the statement blocks on a lock held by another session, the actor
        enters BLOCKED and ``{"blocked": True, ...}`` is returned; the caller
        must later call :meth:`try_unblock`.
        """
        if self.state != ActorState.AT_GATE or self.gate is None:
            raise InfrastructureError(f"actor {self.name}: grant in state {self.state}")
        assert self.conn is not None
        self._pending_gate = self.gate
        if self._on_execute is not None:  # the single place where an actor's statement reaches the database
            self._on_execute(self.name, self.gate)
        self.conn.start(self.gate.raw_sql, self.gate.exec_params)
        return self._wait_stmt_done()

    def _wait_stmt_done(self) -> dict:
        gate = self._pending_gate
        assert self.conn is not None
        deadline = time.monotonic() + STEP_TIMEOUT_S
        last = time.monotonic()
        max_gap = 0.0  # longest gap between two iterations of this loop (a harness or host stall)
        while True:
            now = time.monotonic()
            max_gap, last = max(max_gap, now - last), now
            res = self.conn.wait(0.01)
            if res is not None:
                rec = self._record_stmt(gate, res)
                if res.failure is not None:  # the statement was cancelled; the actor gets no result
                    raise ActorFailure(f"actor {self.name}: the result of {gate.sql!r} was refused: {res.failure}",
                                       loaded=self._loaded)
                self._send({"type": "result", "seq": gate.seq, "rows": res.rows, "rowcount": res.rowcount,
                            "error": res.error, "exception": res.exception, "in_txn": res.in_txn})
                self._await_next()
                return rec
            if self.backend_pid is not None:
                blockers = self._probe(self.backend_pid)
                if blockers:
                    self.state = ActorState.BLOCKED
                    return {"blocked": True, "blockers": blockers, "sql": gate.sql, "cls": gate.cls}
            if time.monotonic() > deadline:
                raise self._step_timeout(gate, max(max_gap, time.monotonic() - last))

    def _step_timeout(self, gate: Gate, max_gap: float) -> StepTimeout:
        """The granted statement exceeded the step timeout: gather evidence, end the statement and the actor,
        and return the exception to raise. The actor receives nothing (its process is killed), so no
        wall-clock outcome enters the trace. Evidence is gathered in this order because each step disturbs the
        next: the server's view of the running statement first, then a cancel request."""
        assert self.conn is not None
        evidence: dict = {"step_timeout_s": STEP_TIMEOUT_S, "max_loop_gap_s": round(max_gap, 3),
                          "stall_gap_s": STEP_STALL_GAP_S, "activity": None, "cancel": None}
        pid = self.backend_pid
        if self._activity is not None and pid is not None:
            try:
                evidence["activity"] = self._activity(pid)
            except Exception as exc:  # noqa: BLE001 - an unanswered query is evidence of an unhealthy server
                evidence["activity_error"] = f"{type(exc).__name__}: {str(exc).strip()[:200]}"
        self.conn.cancel()
        res = self.conn.wait(STEP_CANCEL_GRACE_S)
        if res is None:
            evidence["cancel"] = f"not honoured within {STEP_CANCEL_GRACE_S}s"
        elif res.error is not None:
            evidence["cancel"] = "cancelled" if res.error.get("sqlstate") == "57014" else \
                f"error (SQLSTATE {res.error.get('sqlstate')}): {res.error.get('message', '')[:200]}"
        elif res.exception is not None:
            evidence["cancel"] = f"exception: {res.exception.get('type')}"
        elif res.failure is not None:
            evidence["cancel"] = f"result refused: {res.failure}"
        else:
            evidence["cancel"] = "completed"
        # End the actor as a crash would: kill the process and close the connection (terminating the backend if
        # the statement is still running), which rolls back its transaction and releases its locks.
        self.kill()
        attributable, reason = step_timeout_attribution(evidence, STEP_TIMEOUT_S, STEP_STALL_GAP_S)
        verdict = "attributed to the statement" if attributable else "not attributed to the candidate"
        return StepTimeout(f"actor {self.name}: statement neither completed nor blocked within {STEP_TIMEOUT_S}s: "
                           f"{gate.sql} ({verdict}: {reason})",
                           evidence=evidence, attributable=attributable, reason=reason, loaded=self._loaded)

    def still_blocked(self) -> bool:
        """True if a BLOCKED actor is still waiting on another session's lock."""
        if self.state != ActorState.BLOCKED:
            return False
        if self.conn is None or not self.conn.busy:
            return False
        return bool(self._probe(self.backend_pid)) if self.backend_pid else False

    def try_unblock(self) -> dict | None:
        """If no longer blocked, wait for the statement to finish and return its record."""
        if self.state != ActorState.BLOCKED or self.still_blocked():
            return None
        self.state = ActorState.AT_GATE  # transiently; _wait_stmt_done resolves it
        return self._wait_stmt_done()

    def _record_stmt(self, gate: Gate, res: StatementResult) -> dict:
        p = self.progress
        assert p is not None
        p.statements += 1
        err = res.error
        if res.exception is not None:
            err = {"sqlstate": None, "message": f"{res.exception['type']}: {res.exception['message']}"}
        if res.failure is not None:
            err = {"sqlstate": None, "message": f"result refused by the harness: {res.failure}"}
        if err is None:
            if gate.cls in ("write", "ddl"):
                p.writes += 1
                if not gate.in_txn:
                    p.commits += 1  # autocommit statement
            elif gate.cls == "commit":
                p.commits += 1
        rec = {"sql": gate.sql, "params": gate.params, "cls": gate.cls, "in_txn": gate.in_txn,
               "rowcount": res.rowcount, "error": err}
        p.stmt_log.append(rec)
        return rec

    def deliver(self) -> dict:
        """Release the response to the client; returns the outcome."""
        if self.state != ActorState.AT_RESPOND or self.progress is None:
            raise InfrastructureError(f"actor {self.name}: deliver in state {self.state}")
        outcome = self.progress.outcome
        self._send({"type": "grant"})
        self.state = ActorState.IDLE
        return outcome  # type: ignore[return-value]
