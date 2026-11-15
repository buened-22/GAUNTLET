"""Adversarial Execution Engine: interprets directives against actor processes.

The controller is single-threaded and the only component that grants
statements, so the order in which statements reach PostgreSQL is fully
determined by the directive list, the seeded random choices of
``interleave``, and the implementation's own behaviour. Lock waits are
detected logically (``pg_blocking_pids``) and resolved deterministically
after every step that can release locks.

Each step appends an event to the trace. Events carry a monotonically
increasing ``seq`` (the logical clock used by the oracle) and a wall-clock
offset ``t_ms`` (observational only, excluded from reproducibility hashes).
"""

from __future__ import annotations

import random
import time
from typing import Any

import psycopg

from gauntlet.actor.proxy import ActorProxy, ActorState, ProcessPool
from gauntlet.engine.observe import ObservationObstructed, backend_activity, backend_alive, blocking_pids, capture
from gauntlet.oracle.ledger import Completion, Invocation, Ledger
from gauntlet.pg.provision import InfrastructureError

MAX_INTERLEAVE_STEPS = 5000


class RunTimeout(InfrastructureError):
    """The run exceeded its configured wall-clock limit (a harness limit, never a pass)."""


class RunLimitExceeded(InfrastructureError):
    """The run exceeded a configured size limit, e.g. the maximum number of trace events."""


class ScheduleError(InfrastructureError):
    """The schedule cannot be executed as written (e.g. an explicit choice that is not runnable)."""


class Controller:
    def __init__(self, impl_id: str, dsn: str, monitor: psycopg.Connection, ops: dict[str, dict],
                 pool: ProcessPool | None = None, deadline: float | None = None, max_events: int | None = None,
                 control: psycopg.Connection | None = None):
        self.impl_id = impl_id
        self.pool = pool or ProcessPool()
        self.dsn = dsn
        # ``monitor`` reads snapshots of the run database. ``control`` (cluster-wide lock probe, liveness,
        # backend termination, statement activity) defaults to the same connection. For external candidates the
        # runner passes a restricted observer as ``monitor`` and a separate control connection that never
        # connects to the run database (audit finding A-02).
        self.monitor = monitor
        self.control = control if control is not None else monitor
        self.restricted_observer = control is not None
        self.ops = ops  # logical_id -> {op, args, role}
        self.ledger = Ledger()
        self.events: list[dict] = []
        self.snapshots: list[dict] = []
        self.repeats: list[dict] = []
        self.actors: dict[str, ActorProxy] = {}
        self.inflight: dict[str, str] = {}  # actor -> attempt_id
        self.attempts: dict[str, list[str]] = {}  # logical_id -> attempt ids
        self.outcomes: dict[str, dict] = {}  # attempt_id -> outcome
        self.registered: set[str] = set()
        self.inconclusive: list[str] = []
        self.notes: list[str] = []
        self._seq = 0
        self._t0 = time.perf_counter()
        self._directive = -1
        self._protocol_n = 0
        # Interleaving decisions per interleave directive (index -> list of steps). Kept outside the
        # event trace so recording them does not change trace hashes.
        self.decisions: dict[int, list[dict]] = {}
        # Every statement executed on any actor connection, client and protocol operations alike, in execution
        # order (audit finding A-01). Client statements also appear in the event trace; protocol statements
        # (instance registration) do not, so that trace hashes stay comparable with recorded evidence. Kept
        # outside the event trace for the same reason.
        self.statement_log: list[dict] = []
        self.deadline = deadline  # time.monotonic() value, or None
        self.max_events = max_events

    # -- trace ---------------------------------------------------------------
    def _event(self, kind: str, actor: str | None = None, **data: Any) -> int:
        if self.deadline is not None and time.monotonic() > self.deadline:
            raise RunTimeout("run exceeded its wall-clock limit")
        if self.max_events is not None and self._seq >= self.max_events:
            raise RunLimitExceeded(f"run exceeded the limit of {self.max_events} trace events")
        self._seq += 1
        self.events.append({"seq": self._seq, "directive": self._directive, "kind": kind, "actor": actor,
                            "t_ms": round((time.perf_counter() - self._t0) * 1000, 3), **data})
        return self._seq

    # -- actor lifecycle -------------------------------------------------------
    def _probe(self, pid: int) -> list[int]:
        return blocking_pids(self.control, pid)

    def _terminate_backend(self, pid: int) -> None:
        """End an actor's database session while one of its statements is still running (crash emulation:
        the harness, not the actor process, owns the connection)."""
        self.control.execute("SELECT pg_catalog.pg_terminate_backend(%s)", (pid,))

    def _activity(self, pid: int) -> dict | None:
        """The server's view of an actor's backend (evidence when a statement exceeds the step timeout)."""
        return backend_activity(self.control, pid)

    def _log_statement(self, actor: str, gate) -> None:
        self.statement_log.append({"after_seq": self._seq, "actor": actor, "op_id": gate.op_id, "seq": gate.seq,
                                   "sql": gate.sql, "params": gate.params, "cls": gate.cls})

    def _spawn(self, actor: str, role: str) -> None:
        proxy = self.actors.get(actor) or ActorProxy(actor, role, self.impl_id, self.dsn, self._probe, self.pool,
                                                     terminate_backend=self._terminate_backend,
                                                     on_execute=self._log_statement,
                                                     activity_probe=self._activity)
        self.actors[actor] = proxy
        proxy.spawn()
        proxy.wait_hello()
        self._event("spawn", actor, role=role, incarnation=proxy.incarnation)
        if role in ("v1", "v2"):
            self._protocol(actor, "app_register", {"instance_id": actor, "version": role})
            self.registered.add(actor)

    def _protocol(self, actor: str, method: str, args: dict) -> dict:
        """Run a protocol (non-client) operation to completion and require success."""
        self._protocol_n += 1
        op_id = f"P{self._protocol_n:03d}:{method}:{actor}"
        proxy = self.actors[actor]
        self._event("protocol_invoke", actor, op_id=op_id, method=method, args=args)
        proxy.submit(op_id, method, args)
        while proxy.state == ActorState.AT_GATE:
            rec = proxy.grant()
            if rec.get("blocked"):
                self._event("blocked", actor, op_id=op_id, **rec)
                self._wait_unblock(proxy)
            self._resolve_blocked()
        outcome = proxy.deliver()
        self._event("protocol_respond", actor, op_id=op_id, outcome=outcome)
        if outcome["status"] != "ok":
            raise InfrastructureError(f"protocol operation {method} failed on {actor}: {outcome}")
        return outcome

    def _wait_unblock(self, proxy: ActorProxy) -> None:
        deadline = time.monotonic() + 30
        while proxy.state == ActorState.BLOCKED:
            rec = proxy.try_unblock()
            if rec is not None and not rec.get("blocked"):
                self._event("unblocked", proxy.name, **rec)
                return
            if time.monotonic() > deadline:
                raise InfrastructureError(f"{proxy.name}: protocol operation blocked indefinitely")
            time.sleep(0.01)

    # -- client operations -----------------------------------------------------
    def _invoke(self, actor: str, logical_id: str, retry: bool = False, fault: str | None = None) -> str:
        proxy = self._require(actor)
        if proxy.state != ActorState.IDLE:
            raise InfrastructureError(f"schedule error: {actor} is busy ({proxy.state}) at call {logical_id}")
        spec = self.ops[logical_id]
        n = len(self.attempts.setdefault(logical_id, [])) + 1
        attempt_id = f"{logical_id}.a{n}"
        self.attempts[logical_id].append(attempt_id)
        method = f"{proxy.role}_{spec['op']}" if proxy.role in ("v1", "v2") else f"migrator_{spec['op']}"
        seq = self._event("invoke", actor, attempt_id=attempt_id, logical_id=logical_id, op=spec["op"],
                          args=spec["args"], retry=retry, fault=fault)
        self.ledger.append_invocation(Invocation(
            seq=seq, attempt_id=attempt_id, logical_id=logical_id, actor=actor, role=proxy.role,
            op=spec["op"], args=spec["args"], registered=actor in self.registered))
        self.inflight[actor] = attempt_id
        proxy.submit(attempt_id, method, spec["args"])
        return attempt_id

    def _require(self, actor: str) -> ActorProxy:
        proxy = self.actors.get(actor)
        if proxy is None or proxy.state == ActorState.DEAD:
            raise InfrastructureError(f"schedule error: actor {actor} is not running")
        return proxy

    def _grant(self, proxy: ActorProxy) -> dict:
        gate = proxy.gate
        rec = proxy.grant()
        if rec.get("blocked"):
            self._event("blocked", proxy.name, attempt_id=gate.op_id if gate else None,
                        sql=rec["sql"], cls=rec["cls"], blockers=rec["blockers"])
        else:
            self._event("stmt", proxy.name, attempt_id=gate.op_id if gate else None, **rec)
        self._resolve_blocked()
        return rec

    def _deliver(self, proxy: ActorProxy, lost: bool = False, fault: str | None = None) -> dict:
        attempt_id = self.inflight.pop(proxy.name)
        outcome = proxy.deliver()
        self.outcomes[attempt_id] = outcome
        if lost:
            seq = self._event("respond", proxy.name, attempt_id=attempt_id, delivered=False, fault=fault,
                              outcome=outcome)
            self.ledger.append_completion(Completion(seq=seq, attempt_id=attempt_id, status="uncertain",
                                                     reason="response_lost"))
        else:
            seq = self._event("respond", proxy.name, attempt_id=attempt_id, delivered=True, outcome=outcome)
            if outcome["status"] == "ok":
                self.ledger.append_completion(Completion(seq=seq, attempt_id=attempt_id, status="ok",
                                                         reason="delivered", result=outcome.get("result")))
            else:
                self.ledger.append_completion(Completion(seq=seq, attempt_id=attempt_id, status="failed",
                                                         reason="delivered", error=outcome.get("error")))
        self._resolve_blocked()
        return outcome

    def _resolve_blocked(self) -> None:
        """Deterministically let any actor whose lock was released finish its statement."""
        progress = True
        while progress:
            progress = False
            for name in sorted(self.actors):
                proxy = self.actors[name]
                if proxy.state != ActorState.BLOCKED:
                    continue
                rec = proxy.try_unblock()
                if rec is None:
                    continue
                if rec.get("blocked"):  # woke up, then waited on another lock
                    self._event("blocked", name, attempt_id=self.inflight.get(name), sql=rec["sql"],
                                cls=rec["cls"], blockers=rec["blockers"])
                    continue
                self._event("unblocked", name, attempt_id=self.inflight.get(name), **rec)
                progress = True

    def _step(self, proxy: ActorProxy) -> str:
        """Advance an actor by one step. Returns 'stmt', 'blocked', 'done' or 'idle'."""
        if proxy.state == ActorState.AT_GATE:
            rec = self._grant(proxy)
            return "blocked" if rec.get("blocked") else "stmt"
        if proxy.state == ActorState.AT_RESPOND:
            self._deliver(proxy)
            return "done"
        if proxy.state == ActorState.BLOCKED:
            return "blocked"
        return "idle"

    def _run(self, actor: str) -> str:
        proxy = self._require(actor)
        while True:
            if proxy.state == ActorState.IDLE:
                return "done"
            r = self._step(proxy)
            if r == "blocked":
                return "blocked"
            if r == "done":
                return "done"

    def _run_until(self, actor: str, cond: str, n: int, fault: str | None) -> None:
        proxy = self._require(actor)
        seen_commits = 0
        while True:
            if proxy.state == ActorState.AT_RESPOND or proxy.state == ActorState.IDLE:
                self._event("condition_not_reached", actor, cond=cond, n=n)
                return
            if proxy.state == ActorState.BLOCKED:
                self._event("condition_blocked", actor, cond=cond, n=n)
                return
            gate = proxy.gate
            if cond == "before_write" and gate.cls in ("write", "ddl"):
                break
            if cond == "before_commit" and gate.cls == "commit":
                seen_commits += 1
                if seen_commits >= n:
                    break
                self._grant(proxy)
                continue
            rec = self._grant(proxy)
            if rec.get("blocked"):
                self._event("condition_blocked", actor, cond=cond, n=n)
                return
            if cond == "after_commit" and proxy.progress and proxy.progress.commits >= n:
                break
        self._event("paused", actor, cond=cond, n=n, fault=fault,
                    attempt_id=self.inflight.get(actor), next_sql=proxy.gate.sql if proxy.gate else None)

    def _crash(self, actor: str, fault: str | None) -> None:
        proxy = self._require(actor)
        pid = proxy.backend_pid
        attempt_id = self.inflight.pop(actor, None)
        proxy.kill()
        seq = self._event("crash", actor, fault=fault, attempt_id=attempt_id, backend_pid=None)
        if attempt_id:
            self.ledger.append_completion(Completion(seq=seq, attempt_id=attempt_id, status="uncertain",
                                                     reason="crashed"))
        # Wait until PostgreSQL has terminated the orphaned backend (rolling back
        # any open transaction) so later steps see a settled state.
        if pid is not None:
            deadline = time.monotonic() + 10
            while backend_alive(self.control, pid):
                if time.monotonic() > deadline:
                    self._terminate_backend(pid)
                    self.notes.append(f"backend {pid} of crashed {actor} had to be terminated by the harness")
                    break
                time.sleep(0.01)
        self._resolve_blocked()

    # -- directives ----------------------------------------------------------
    def execute(self, directives: list[dict]) -> None:
        for i, d in enumerate(directives):
            self._directive = i
            getattr(self, f"_d_{d['kind']}")(d)

    def _d_stage(self, d: dict) -> None:
        self._event("stage", None, label=d["label"], note=d.get("note", ""))

    def _d_spawn(self, d: dict) -> None:
        self._spawn(d["actor"], d["role"])

    def _d_restart(self, d: dict) -> None:
        proxy = self.actors[d["actor"]]
        if proxy.state != ActorState.DEAD:
            raise InfrastructureError(f"schedule error: restart of live actor {d['actor']}")
        self._event("restart", d["actor"], recovery=True)
        self._spawn(d["actor"], proxy.role)

    def _d_drain(self, d: dict) -> None:
        actor = d["actor"]
        proxy = self._require(actor)
        if proxy.state != ActorState.IDLE:
            if self._run(actor) != "done":
                raise InfrastructureError(f"cannot drain {actor}: in-flight operation is blocked")
        self._protocol(actor, "app_deregister", {"instance_id": actor})
        self.registered.discard(actor)
        proxy.exit()
        self._event("drain", actor)

    def _d_call(self, d: dict) -> None:
        self._invoke(d["actor"], d["logical_id"], retry=d.get("retry", False), fault=d.get("fault"))

    def _d_run(self, d: dict) -> None:
        self._run(d["actor"])

    def _d_run_until(self, d: dict) -> None:
        self._run_until(d["actor"], d["cond"], d.get("n", 1), d.get("fault"))

    def _d_lose_response(self, d: dict) -> None:
        proxy = self._require(d["actor"])
        while proxy.state == ActorState.AT_GATE:
            if self._grant(proxy).get("blocked"):
                raise InfrastructureError(f"schedule error: {d['actor']} blocked before its response")
        if proxy.state != ActorState.AT_RESPOND:
            raise InfrastructureError(f"schedule error: lose_response on {d['actor']} in state {proxy.state}")
        self._deliver(proxy, lost=True, fault=d.get("fault"))

    def _d_crash(self, d: dict) -> None:
        self._crash(d["actor"], d.get("fault"))

    def _ok(self, logical_id: str) -> bool:
        return any(self.outcomes.get(a, {}).get("status") == "ok" for a in self.attempts.get(logical_id, []))

    def _d_retry_until_ok(self, d: dict) -> None:
        tries = 0
        while not self._ok(d["logical_id"]) and tries < d["max_attempts"]:
            tries += 1
            self._invoke(d["actor"], d["logical_id"], retry=True)
            if self._run(d["actor"]) == "blocked":
                self.inconclusive.append(f"retry of {d['logical_id']} blocked")
                return

    def _d_backfill_until_done(self, d: dict) -> None:
        actor = d["actor"]
        for _ in range(d["max_batches"]):
            lid = f"BF{len(self.ops) + 1:03d}"
            self.ops[lid] = {"op": "backfill_batch", "args": {"batch_size": d["batch_size"]}, "role": "migrator"}
            att = self._invoke(actor, lid)
            if self._run(actor) == "blocked":
                self.inconclusive.append(f"backfill batch {lid} blocked")
                return
            out = self.outcomes.get(att, {})
            if out.get("status") == "ok" and out.get("result", {}).get("done"):
                return
            if out.get("status") != "ok":
                self._event("note", actor, message=f"backfill batch {lid} failed: {out.get('error')}")
                return
        self._event("note", actor, message=f"backfill not done after {d['max_batches']} batches")

    def _d_repeat(self, d: dict) -> None:
        pre = self._snapshot(f"before repeat of {d['logical_id']}")
        att = self._invoke(d["actor"], d["logical_id"], retry=True, fault=d.get("fault"))
        if self._run(d["actor"]) == "blocked":
            self.inconclusive.append(f"repeat of {d['logical_id']} blocked")
            return
        post = self._snapshot(f"after repeat of {d['logical_id']}")
        self.repeats.append({"logical_id": d["logical_id"], "attempt_id": att, "pre": pre, "post": post,
                             "op": self.ops[d["logical_id"]]["op"]})

    def _d_interleave(self, d: dict) -> None:
        """Statement-level interleaving of the actors' queued operations.

        Which runnable actor takes the next step is decided by, in order of precedence:

        * ``choices`` (explicit list of actor names, consumed one per step). ``choice_mode``
          ``"strict"`` (default) makes a choice that is not runnable a schedule error;
          ``"lenient"`` skips such choices. Once the list is exhausted, the remaining steps use the
          deterministic non-preemptive default: keep running the actor that took the previous step
          if it is still runnable, otherwise the first runnable actor in name order.
        * otherwise the seeded random choice of the benchmark (``seed``), unchanged since v0.1.0.

        Every step is recorded in ``self.decisions[directive index]`` (not in the event trace).
        """
        explicit = d.get("choices")
        strict = d.get("choice_mode", "strict") == "strict"
        rng = None if explicit is not None else random.Random(d["seed"])
        pending = list(explicit or [])
        queues = {a: list(q) for a, q in d["queues"].items()}
        log = self.decisions.setdefault(self._directive, [])
        prev = None
        for step in range(MAX_INTERLEAVE_STEPS):
            runnable = []
            for a in sorted(queues):
                proxy = self._require(a)
                if proxy.state in (ActorState.AT_GATE, ActorState.AT_RESPOND):
                    runnable.append(a)
                elif proxy.state == ActorState.IDLE and queues[a]:
                    runnable.append(a)
            if not runnable:
                if pending and strict:
                    raise ScheduleError(f"interleave at directive {self._directive}: {len(pending)} explicit "
                                        f"choice(s) left but no actor is runnable")
                if any(self.actors[a].state == ActorState.BLOCKED for a in queues):
                    self.inconclusive.append("interleave: all remaining actors blocked")
                    self._event("stuck", None, actors=sorted(queues))
                return
            if rng is not None:
                a, source = rng.choice(runnable), "seed"
            else:
                a = source = None
                while pending:
                    cand = pending.pop(0)
                    if cand in runnable:
                        a, source = cand, "choice"
                        break
                    if strict:
                        raise ScheduleError(f"interleave at directive {self._directive}, step {step}: "
                                            f"explicit choice {cand!r} is not runnable (runnable: {runnable})")
                if a is None:
                    a, source = (prev if prev in runnable else runnable[0]), "default"
            proxy = self.actors[a]
            entry = {"step": step, "runnable": runnable, "chosen": a, "source": source,
                     "preemption": prev is not None and a != prev and prev in runnable}
            if proxy.state == ActorState.IDLE:
                lid = queues[a].pop(0)
                entry.update(action="invoke", logical_id=lid)
                log.append(entry)
                self._invoke(a, lid, fault=d.get("fault"))
            else:
                attempt = self.inflight.get(a)
                entry.update(action="respond" if proxy.state == ActorState.AT_RESPOND else "stmt",
                             logical_id=attempt.rsplit(".a", 1)[0] if attempt else None)
                log.append(entry)
                self._step(proxy)
            prev = a
        self.inconclusive.append("interleave exceeded step bound")

    def _d_snapshot(self, d: dict) -> None:
        self._snapshot(d["label"])

    def _snapshot(self, label: str) -> int:
        try:
            state = capture(self.monitor)
        except psycopg.errors.LockNotAvailable as exc:  # an actor paused while holding a lock: a deadlock
            raise InfrastructureError(f"snapshot '{label}' could not read the database: a lock held by a paused "
                                      f"actor did not become free ({exc.sqlstate})") from exc
        except (psycopg.errors.InsufficientPrivilege, psycopg.errors.UndefinedTable) as exc:
            if not self.restricted_observer:
                raise
            # The candidate owns the observed objects: it revoked the observer's access, enabled row-level
            # security (an error under row_security = off, never silent filtering) or removed a table.
            raise ObservationObstructed(f"snapshot '{label}' could not read the database as the harness's "
                                        f"observer ({exc.sqlstate}: {str(exc).strip().splitlines()[0][:200]})") from exc
        except ObservationObstructed as exc:
            raise ObservationObstructed(f"snapshot '{label}': {exc}") from exc
        seq = self._event("snapshot", None, label=label, index=len(self.snapshots))
        self.snapshots.append({"label": label, "seq": seq, **state})
        return len(self.snapshots) - 1

    # -- end of run --------------------------------------------------------------
    def settle(self) -> None:
        """Complete every in-flight operation (round-robin), then stop all actors."""
        self._directive = -2
        for _ in range(MAX_INTERLEAVE_STEPS):
            busy = [a for a in sorted(self.inflight) if self.actors[a].state != ActorState.DEAD]
            if not busy:
                break
            progressed = False
            for a in busy:
                proxy = self.actors[a]
                if proxy.state in (ActorState.AT_GATE, ActorState.AT_RESPOND):
                    r = self._step(proxy)
                    progressed = progressed or r != "blocked"
            self._resolve_blocked()
            if not progressed and all(self.actors[a].state == ActorState.BLOCKED for a in busy):
                self.inconclusive.append(f"settle: operations stuck on locks: {busy}")
                self._event("stuck", None, actors=busy)
                break
        for a, attempt_id in list(self.inflight.items()):
            self.actors[a].kill()
            seq = self._event("abandon", a, attempt_id=attempt_id)
            self.ledger.append_completion(Completion(seq=seq, attempt_id=attempt_id, status="uncertain",
                                                     reason="incomplete"))
            self.inflight.pop(a)
        self._snapshot("final")

    def abort(self) -> None:
        """Called from a watchdog thread when the run's deadline passes: kill every actor process and
        cancel the monitor's current query, so a blocking wait in the controller fails promptly."""
        self.aborted = True
        for proxy in list(self.actors.values()):
            try:
                proxy.abort()  # kills the process and cancels a running statement; never closes the connection
            except Exception:  # noqa: BLE001
                pass
        for conn in {id(c): c for c in (self.monitor, self.control)}.values():
            try:
                conn.cancel_safe()
            except Exception:
                pass

    def shutdown(self) -> None:
        """Stop every actor and the pool. A failure while stopping one actor never prevents stopping the
        others or closing the pool; failures that remain after a hard kill are recorded in ``notes``."""
        try:
            for proxy in list(self.actors.values()):
                try:
                    if proxy.state != ActorState.DEAD:
                        proxy.exit()
                    elif proxy.child is not None:
                        proxy.child.release()  # sandboxed actors: remove workspace / job (no-op otherwise)
                except Exception:  # noqa: BLE001
                    try:
                        proxy.kill()
                    except Exception as exc:  # noqa: BLE001
                        self.notes.append(f"cleanup: could not stop actor {proxy.name}: {type(exc).__name__}: {exc}")
        finally:
            try:
                self.pool.close()
            except Exception as exc:  # noqa: BLE001 - the run's observations are complete; record, don't mask
                self.notes.append(f"cleanup: could not close the actor pool: {type(exc).__name__}: {exc}")
            for proxy in list(self.actors.values()):  # connections that could not be shut down, during or after the run
                self.notes += [f"cleanup: {e}" for e in getattr(proxy, "cleanup_errors", [])]
