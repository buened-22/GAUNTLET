"""Append-only client-side ledger of invocations and their outcomes.

The ledger is written by the harness (acting as the client), never by the
implementation under evaluation. It records what clients *asked for* and
what they *were told*. Outcomes:

    ok         the client received a success response
    failed     the client received an explicit error response
    uncertain  the client received no response (actor crashed, response lost,
               or the operation never completed); it may or may not have
               taken effect
"""

from __future__ import annotations

import copy
from dataclasses import asdict, dataclass
from typing import Any


@dataclass(frozen=True)
class Invocation:
    seq: int
    attempt_id: str
    logical_id: str
    actor: str
    role: str  # v1 | v2 | migrator
    op: str
    args: dict
    registered: bool  # actor was a registered app instance when invoked


@dataclass(frozen=True)
class Completion:
    seq: int
    attempt_id: str
    status: str  # ok | failed | uncertain
    reason: str  # delivered | response_lost | crashed | incomplete
    result: Any = None
    error: dict | None = None


class LedgerError(RuntimeError):
    pass


class Ledger:
    def __init__(self) -> None:
        self._records: list[tuple[str, Invocation | Completion]] = []
        self._invoked: dict[str, Invocation] = {}
        self._completed: dict[str, Completion] = {}
        self._last_seq = -1

    def _check_seq(self, seq: int) -> None:
        if seq <= self._last_seq:
            raise LedgerError(f"non-monotonic ledger sequence {seq} <= {self._last_seq}")
        self._last_seq = seq

    def append_invocation(self, inv: Invocation) -> None:
        if inv.attempt_id in self._invoked:
            raise LedgerError(f"duplicate invocation {inv.attempt_id}")
        self._check_seq(inv.seq)
        inv = Invocation(**{**asdict(inv), "args": copy.deepcopy(inv.args)})
        self._invoked[inv.attempt_id] = inv
        self._records.append(("invocation", inv))

    def append_completion(self, comp: Completion) -> None:
        if comp.attempt_id not in self._invoked:
            raise LedgerError(f"completion for unknown attempt {comp.attempt_id}")
        if comp.attempt_id in self._completed:
            raise LedgerError(f"attempt {comp.attempt_id} already completed")
        if comp.status not in ("ok", "failed", "uncertain"):
            raise LedgerError(f"bad status {comp.status}")
        self._check_seq(comp.seq)
        self._completed[comp.attempt_id] = comp
        self._records.append(("completion", comp))

    def is_open(self, attempt_id: str) -> bool:
        return attempt_id in self._invoked and attempt_id not in self._completed

    def records(self) -> list[dict]:
        return [{"type": t, **asdict(r)} for t, r in self._records]

    def attempts(self) -> list[dict]:
        """Joined view: one dict per attempt, in invocation order."""
        out = []
        for t, r in self._records:
            if t != "invocation":
                continue
            c = self._completed.get(r.attempt_id)
            out.append({
                **asdict(r), "invoke_seq": r.seq,
                "complete_seq": c.seq if c else None,
                "status": c.status if c else "open",
                "reason": c.reason if c else None,
                "result": c.result if c else None,
                "error": c.error if c else None,
            })
        return out

    @staticmethod
    def from_records(records: list[dict]) -> "Ledger":
        led = Ledger()
        for rec in records:
            rec = dict(rec)
            t = rec.pop("type")
            if t == "invocation":
                led.append_invocation(Invocation(**rec))
            else:
                led.append_completion(Completion(**rec))
        return led
