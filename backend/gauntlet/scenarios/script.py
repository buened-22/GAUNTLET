"""Schedule definition language.

A scenario's ``build(seed)`` returns the initial database state and an
ordered list of *directives* (plain JSON-serialisable dicts). The directive
list is a pure function of (scenario id, seed, benchmark version) and is
independent of the implementation, so its hash identifies the logical
schedule. How each directive unfolds at statement level is implementation
dependent and is recorded separately in the execution trace.

Directive kinds
---------------
stage(label)                       phase marker (P0..P5)
spawn(actor, role)                 start an actor process; app roles register
drain(actor)                       app deregisters, then the process exits
call(actor, logical_id)            invoke a new attempt of a logical operation
run(actor)                         grant statements until the response is delivered
                                   (returns early if the actor blocks on a lock)
run_until(actor, cond, n)          cond in {before_write, before_commit, after_commit}
lose_response(actor)               run to the response, then drop it (client times out)
crash(actor)                       hard-kill the actor process
restart(actor)                     start a fresh process for the actor
retry_until_ok(actor, logical_id, max_attempts)
backfill_until_done(actor, batch_size, max_batches)
repeat(actor, logical_id)          re-execute a completed step between two snapshots
interleave(queues, seed)           seeded random statement-level interleaving
snapshot(label)                    oracle captures raw database state
"""

from __future__ import annotations

import random
from typing import Any

from gauntlet.money import format_cents

CLIENT_OPS = ("create_order", "set_total", "add_charge", "get_order")
MIGRATION_OPS = ("expand", "backfill_batch", "finalize", "contract")


def float_hostile(cents: int) -> bool:
    """True if converting this amount through binary floating point truncates it."""
    return int(float(format_cents(cents)) * 100) != cents


class Script:
    def __init__(self, seed: int, scenario_id: str):
        self.rng = random.Random(f"{scenario_id}:{seed}")
        self.directives: list[dict] = []
        self.ops: dict[str, dict] = {}  # logical_id -> {op, args, role}
        self._n = 0
        self.roles: dict[str, str] = {}

    # -- helpers -------------------------------------------------------------
    def amount(self, lo: int = 100, hi: int = 50_000, hostile_p: float = 0.4) -> str:
        """Random amount; with probability hostile_p, one that floats cannot represent."""
        if self.rng.random() < hostile_p:
            while True:
                c = self.rng.randint(lo, hi)
                if float_hostile(c):
                    return format_cents(c)
        return format_cents(self.rng.randint(lo, hi))

    def _lid(self) -> str:
        self._n += 1
        return f"L{self._n:03d}"

    def _add(self, kind: str, **kw: Any) -> dict:
        d = {"kind": kind, **kw}
        self.directives.append(d)
        return d

    def define(self, actor: str, op: str, **args: Any) -> str:
        lid = self._lid()
        if op in ("create_order", "set_total", "add_charge"):
            args = {**args, "key": f"key-{lid}"}
        self.ops[lid] = {"op": op, "args": args, "role": self.roles[actor]}
        return lid

    # -- directives ----------------------------------------------------------
    def stage(self, label: str, note: str = "") -> None:
        self._add("stage", label=label, note=note)

    def spawn(self, actor: str, role: str) -> None:
        self.roles[actor] = role
        self._add("spawn", actor=actor, role=role)

    def drain(self, actor: str) -> None:
        self._add("drain", actor=actor)

    def call(self, actor: str, op: str, **args: Any) -> str:
        lid = self.define(actor, op, **args)
        self._add("call", actor=actor, logical_id=lid)
        return lid

    def run(self, actor: str) -> None:
        self._add("run", actor=actor)

    def op(self, actor: str, op: str, **args: Any) -> str:
        lid = self.call(actor, op, **args)
        self.run(actor)
        return lid

    def run_until(self, actor: str, cond: str, n: int = 1, fault: str | None = None) -> None:
        assert cond in ("before_write", "before_commit", "after_commit")
        self._add("run_until", actor=actor, cond=cond, n=n, fault=fault)

    def lose_response(self, actor: str, fault: str = "F1") -> None:
        self._add("lose_response", actor=actor, fault=fault)

    def crash(self, actor: str, fault: str) -> None:
        self._add("crash", actor=actor, fault=fault)

    def restart(self, actor: str) -> None:
        self._add("restart", actor=actor, recovery=True)

    def retry(self, actor: str, logical_id: str, fault: str | None = "F2", run: bool = True) -> None:
        """Retry an uncertain operation with the same idempotency key.

        With ``run=False`` the retry is only invoked, so a following ``interleave`` can schedule
        it statement by statement together with other actors' requests.
        """
        self._add("call", actor=actor, logical_id=logical_id, retry=True, fault=fault)
        if run:
            self.run(actor)

    def run_ops(self, actor: str, logical_ids: list[str]) -> None:
        """Invoke and run already-defined operations one after another."""
        for lid in logical_ids:
            self._add("call", actor=actor, logical_id=lid)
            self.run(actor)

    def retry_until_ok(self, actor: str, logical_id: str, max_attempts: int = 3) -> None:
        self._add("retry_until_ok", actor=actor, logical_id=logical_id, max_attempts=max_attempts, recovery=True)

    def backfill_until_done(self, actor: str, batch_size: int, max_batches: int = 50) -> None:
        self._add("backfill_until_done", actor=actor, batch_size=batch_size, max_batches=max_batches)

    def repeat(self, actor: str, logical_id: str, fault: str = "F5") -> None:
        self._add("repeat", actor=actor, logical_id=logical_id, fault=fault)

    def interleave(self, queues: dict[str, list[str]], fault: str | None = None) -> None:
        self._add("interleave", queues=queues, seed=self.rng.randint(0, 2**31 - 1), fault=fault)

    def snapshot(self, label: str) -> None:
        self._add("snapshot", label=label)

    # -- workload ------------------------------------------------------------
    def random_ops(self, actor: str, ids: list[int], n: int, *, writes_only: bool = False,
                   hostile_p: float = 0.4) -> list[str]:
        out = []
        for _ in range(n):
            r = self.rng.random()
            oid = self.rng.choice(ids)
            if r < 0.35:
                out.append(self.define(actor, "add_charge", order_id=oid,
                                       amount=self.amount(1, 5_000, hostile_p)))
            elif r < 0.65 or writes_only:
                out.append(self.define(actor, "set_total", order_id=oid, total=self.amount(hostile_p=hostile_p)))
            else:
                out.append(self.define(actor, "get_order", order_id=oid))
        return out
