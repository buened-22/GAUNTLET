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
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Callable

from gauntlet.config import CHILD_LOCK_TIMEOUT_MS, STEP_TIMEOUT_S
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
    sql: str
    params: Any
    cls: str
    in_txn: bool


@dataclass
class OpProgress:
    op_id: str
    statements: int = 0
    commits: int = 0
    writes: int = 0
    outcome: dict | None = None
    stmt_log: list[dict] = field(default_factory=list)


MAX_PROTOCOL_LINE = 4 * 1024 * 1024  # sandboxed actors: longer stdout lines are a protocol violation


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
                    try:
                        self.q.put(json.loads(line))
                    except json.JSONDecodeError:
                        self.q.put({"type": "garbage", "line": line[:500]})
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
                try:
                    self.q.put(json.loads(line))
                except json.JSONDecodeError:
                    self.q.put({"type": "garbage", "line": line[:500]})
        self.q.put({"type": "eof"})

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
        for c in self._free:
            c.kill()
        self._free.clear()


class ActorProxy:
    def __init__(self, name: str, role: str, impl_id: str, dsn: str,
                 blocking_probe: Callable[[int], list[int]], pool: ProcessPool | None = None):
        self.name = name
        self.role = role  # "v1" | "v2" | "migrator"
        self.impl_id = impl_id
        self.dsn = dsn
        self._probe = blocking_probe
        self._pool = pool or ProcessPool()
        self.state = ActorState.DEAD
        self.child: ChildProcess | None = None
        self.backend_pid: int | None = None
        self.gate: Gate | None = None
        self.progress: OpProgress | None = None
        self.incarnation = 0

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
        init = {"type": "init", "impl": self.impl_id, "dsn": self.dsn, "name": self.name,
                "lock_timeout_ms": CHILD_LOCK_TIMEOUT_MS}
        if self.child.sandbox is not None:
            from gauntlet.candidates.snapshots import snapshots_root
            init["sandbox"] = {"workspace": str(self.child.sandbox.workspace), "snapshots_root": str(snapshots_root())}
        self._send(init)

    def wait_hello(self) -> None:
        msg = self._recv(timeout=STEP_TIMEOUT_S * 3)
        if msg.get("type") != "hello":
            raise InfrastructureError(f"actor {self.name}: bad hello {msg}")
        self.backend_pid = msg["backend_pid"]
        self.state = ActorState.IDLE

    def _send(self, msg: dict) -> None:
        assert self.proc is not None and self.proc.stdin is not None
        try:
            self.proc.stdin.write(json.dumps(msg) + "\n")
            self.proc.stdin.flush()
        except OSError as exc:
            raise ActorFailure(f"actor {self.name}: pipe closed ({exc}); stderr={self.stderr_tail()}",
                               loaded=self.backend_pid is not None) from exc

    def _recv(self, timeout: float) -> dict:
        try:
            msg = self._q.get(timeout=timeout)
        except queue.Empty:
            raise ActorFailure(f"actor {self.name}: no message within {timeout}s; stderr={self.stderr_tail()}",
                               loaded=self.backend_pid is not None) from None
        if msg.get("type") in ("eof", "garbage", "fatal"):
            load_failed = msg.get("type") == "fatal" and "could not load implementation" in str(msg.get("error"))
            raise ActorFailure(f"actor {self.name}: died or broke protocol: {msg}; stderr={self.stderr_tail()}",
                               loaded=self.backend_pid is not None or load_failed)
        return msg

    def stderr_tail(self) -> str:
        return " | ".join(self.child.stderr_tail[-8:]) if self.child else ""

    def kill(self) -> None:
        """Hard-kill the process (TerminateProcess / SIGKILL)."""
        if self.child:
            self.child.kill()
        self.state = ActorState.DEAD
        self.gate = None

    def exit(self) -> None:
        if self.proc and self.proc.poll() is None:
            try:
                self._send({"type": "exit"})
                self.proc.wait(timeout=10)
            except (InfrastructureError, subprocess.TimeoutExpired):
                self.proc.kill()
        if self.child is not None:
            self.child.release()
        self.state = ActorState.DEAD

    # -- operation control --------------------------------------------------
    def submit(self, op_id: str, method: str, args: dict) -> None:
        if self.state != ActorState.IDLE:
            raise InfrastructureError(f"actor {self.name} not idle (state={self.state})")
        self.progress = OpProgress(op_id=op_id)
        self._send({"type": "op", "op_id": op_id, "method": method, "args": args})
        self._await_next()

    def _await_next(self) -> None:
        msg = self._recv(timeout=STEP_TIMEOUT_S)
        if msg["type"] == "gate":
            self.gate = Gate(msg["op_id"], msg["seq"], msg["sql"], msg["params"], msg["cls"], msg["in_txn"])
            self.state = ActorState.AT_GATE
        elif msg["type"] == "respond":
            assert self.progress is not None
            self.progress.outcome = msg["outcome"]
            self.gate = None
            self.state = ActorState.AT_RESPOND
        else:
            raise InfrastructureError(f"actor {self.name}: unexpected message {msg}")

    def grant(self) -> dict:
        """Let the pending statement execute. Returns a stmt record.

        If the statement blocks on a lock held by another session, the actor
        enters BLOCKED and ``{"blocked": True, ...}`` is returned; the caller
        must later call :meth:`try_unblock`.
        """
        if self.state != ActorState.AT_GATE or self.gate is None:
            raise InfrastructureError(f"actor {self.name}: grant in state {self.state}")
        self._pending_gate = self.gate
        self._send({"type": "grant"})
        return self._wait_stmt_done()

    def _wait_stmt_done(self) -> dict:
        gate = self._pending_gate
        deadline = time.monotonic() + STEP_TIMEOUT_S
        while True:
            try:
                msg = self._q.get(timeout=0.01)
            except queue.Empty:
                msg = None
            if msg is not None:
                if msg.get("type") != "stmt_done":
                    raise InfrastructureError(f"actor {self.name}: expected stmt_done, got {msg}; stderr={self.stderr_tail()}")
                rec = self._record_stmt(gate, msg)
                self._await_next()
                return rec
            if self.backend_pid is not None:
                blockers = self._probe(self.backend_pid)
                if blockers:
                    self.state = ActorState.BLOCKED
                    return {"blocked": True, "blockers": blockers, "sql": gate.sql, "cls": gate.cls}
            if time.monotonic() > deadline:
                raise InfrastructureError(f"actor {self.name}: statement neither completed nor blocked within "
                                          f"{STEP_TIMEOUT_S}s: {gate.sql}")

    def still_blocked(self) -> bool:
        """True if a BLOCKED actor is still waiting on another session's lock."""
        if self.state != ActorState.BLOCKED:
            return False
        if not self._q.empty():
            return False
        return bool(self._probe(self.backend_pid)) if self.backend_pid else False

    def try_unblock(self) -> dict | None:
        """If no longer blocked, wait for the statement to finish and return its record."""
        if self.state != ActorState.BLOCKED or self.still_blocked():
            return None
        self.state = ActorState.AT_GATE  # transiently; _wait_stmt_done resolves it
        return self._wait_stmt_done()

    def _record_stmt(self, gate: Gate, msg: dict) -> dict:
        p = self.progress
        assert p is not None
        p.statements += 1
        err = msg.get("error")
        if err is None:
            if gate.cls in ("write", "ddl"):
                p.writes += 1
                if not gate.in_txn:
                    p.commits += 1  # autocommit statement
            elif gate.cls == "commit":
                p.commits += 1
        rec = {"sql": gate.sql, "params": gate.params, "cls": gate.cls, "in_txn": gate.in_txn,
               "rowcount": msg.get("rowcount"), "error": err}
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
