"""Per-order linearizability checking against an exact-cents model.

Model (one per order): value is ``ABSENT`` or an integer number of cents.

    create(v): ABSENT -> v            (invalid if present)
    set(v):    x -> v                 (invalid if ABSENT)
    add(d):    x -> x + d             (invalid if ABSENT)
    read:      observes the value     (must equal one of the observed values)

Items to linearise
------------------
* One **effect** per logical write (all attempts sharing an idempotency key):
  mandatory if any attempt was acknowledged, otherwise optional; it may be
  placed inside the execution window of any acknowledged or uncertain attempt.
* One **observation** per successful read and per harness snapshot.

Search: find a sequence of items with strictly increasing points, each inside
one of its allowed open intervals, that the model accepts and that includes
every mandatory item. Points are placed greedily at the earliest feasible
position, which is optimal for a fixed sequence. Time is the global harness
event sequence number, doubled so a snapshot at ``q`` occupies ``(2q-1, 2q+1)``.

Relaxed models are used only to *diagnose* a non-linearizable history.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from functools import lru_cache

ABSENT = "ABSENT"
CORRUPT = "CORRUPT"  # an observed row whose authoritative value is NULL
INF = 10**12


@dataclass(frozen=True)
class Item:
    id: str
    kind: str  # create | set | add | read
    arg: int | None = None
    outs: frozenset | None = None  # acceptable observed values (reads) or expected new value (acked add)
    intervals: tuple[tuple[int, int], ...] = ()
    mandatory: bool = True
    label: str = ""


@dataclass
class History:
    order_id: int
    initial: object  # ABSENT or int cents
    effects: list[dict] = field(default_factory=list)  # logical writes (see build_items)
    observations: list[Item] = field(default_factory=list)


@dataclass
class LinResult:
    ok: bool | None  # None = search budget exhausted (inconclusive)
    explored: int
    witness: list[str] | None = None


RELAXATIONS = {
    "lost_ack": "the state may revert to a value it held earlier (acknowledged writes may be lost)",
    "duplicate": "each write may take effect twice",
    "failed_applied": "failed writes may take effect",
    "rounding": "written amounts may be off by one cent",
}


def build_items(h: History, relax: str | None = None) -> list[Item]:
    """Turn logical writes + observations into search items under a model."""
    items: list[Item] = []
    for eff in h.effects:
        windows = [tuple(a["window"]) for a in eff["attempts"] if a["status"] in ("ok", "uncertain")]
        if relax == "failed_applied":
            windows += [tuple(a["window"]) for a in eff["attempts"] if a["status"] == "failed"]
        if not windows:
            continue
        acked = any(a["status"] == "ok" for a in eff["attempts"])
        mandatory = acked
        outs = None
        if relax is None:
            vals = {a["out"] for a in eff["attempts"] if a["status"] == "ok" and a.get("out") is not None}
            outs = frozenset(vals) if vals else None
        args = [eff["arg"]]
        if relax == "rounding" and eff["kind"] in ("create", "set", "add"):
            args = [eff["arg"] - 1, eff["arg"], eff["arg"] + 1]
        for k, arg in enumerate(args):
            items.append(Item(
                id=f"{eff['logical_id']}#{k}" if len(args) > 1 else eff["logical_id"],
                kind=eff["kind"], arg=arg, outs=outs, intervals=tuple(sorted(windows)),
                mandatory=mandatory if len(args) == 1 else False, label=eff["logical_id"],
            ))
        if relax == "duplicate":
            items.append(Item(id=eff["logical_id"] + "#dup", kind=eff["kind"], arg=eff["arg"], outs=None,
                              intervals=tuple(sorted(windows)), mandatory=False, label=eff["logical_id"]))
    items.extend(h.observations)
    return items


def _rounding_groups(items: list[Item]) -> dict[int, list[int]]:
    """For the rounding relaxation, exactly one variant of each mandatory write must be chosen."""
    groups: dict[str, list[int]] = {}
    for i, it in enumerate(items):
        if "#" in it.id and not it.id.endswith("#dup"):
            groups.setdefault(it.label, []).append(i)
    return {i: idxs for idxs in groups.values() for i in idxs}


MAX_REVERTS = 3


def check(h: History, relax: str | None = None, budget: int = 300_000,
          mandatory_labels: set[str] | None = None) -> LinResult:
    """Search for a linearization. ``ok`` is None if the search budget ran out.

    Under ``lost_ack`` the model may additionally *revert* to any value the
    order held earlier (at most MAX_REVERTS times). This covers both an
    acknowledged write that never took effect and one that took effect and was
    later overwritten with stale data.
    """
    items = build_items(h, relax)
    revert_mode = relax == "lost_ack"
    n = len(items)
    groups = _rounding_groups(items) if relax == "rounding" else {}
    # In the rounding model a logical write is mandatory as a group.
    if mandatory_labels is None:
        mandatory_labels = {e["logical_id"] for e in h.effects
                            if any(a["status"] == "ok" for a in e["attempts"])}
    group_mandatory = {i for i, idxs in groups.items() if items[i].label in mandatory_labels}

    mandatory_mask = 0
    for i, it in enumerate(items):
        if it.mandatory:
            mandatory_mask |= 1 << i
    explored = 0
    exhausted = False

    def apply(it: Item, value):
        if it.kind == "read":
            return value if value in it.outs else None, True
        if it.kind == "create":
            if value != ABSENT:
                return None, False
            new = it.arg
        elif it.kind == "set":
            if value == ABSENT:
                return None, False
            new = it.arg
        elif it.kind == "add":
            if value == ABSENT:
                return None, False
            new = value + it.arg
        else:  # pragma: no cover
            raise ValueError(it.kind)
        if it.outs is not None and new not in it.outs:
            return None, False
        return new, True

    def place(it: Item, tb: int, tk: int):
        best = None
        for s, e in it.intervals:
            if tb < s:
                cand = (s, 1)
            else:
                cand = (tb, tk + 1)
            if cand[0] < e and (best is None or cand < best):
                best = cand
        return best

    def can_still_place(it: Item, tb: int) -> bool:
        return any(max(tb, s) < e for s, e in it.intervals)

    @lru_cache(maxsize=None)
    def dfs(tb: int, tk: int, value, done: int, past: frozenset, reverts: int) -> bool:
        nonlocal explored, exhausted
        explored += 1
        if explored > budget:
            exhausted = True
            return False
        # Success when every mandatory item / group is done.
        if (done & mandatory_mask) == mandatory_mask and all(
            any(done >> j & 1 for j in groups[i]) for i in group_mandatory
        ):
            return True
        # Prune: a mandatory item (or group) that can no longer be placed.
        for i, it in enumerate(items):
            if done >> i & 1:
                continue
            if it.mandatory and not can_still_place(it, tb):
                return False
        for i in group_mandatory:
            if not any(done >> j & 1 for j in groups[i]) and not any(
                can_still_place(items[j], tb) for j in groups[i]
            ):
                return False
        for i, it in enumerate(items):
            if done >> i & 1:
                continue
            if i in groups and any(done >> j & 1 for j in groups[i]):
                continue  # another rounding variant of this write already chosen
            pt = place(it, tb, tk)
            if pt is None:
                continue
            new, valid = apply(it, value)
            if not valid or (it.kind == "read" and new is None):
                continue
            ndone = done | (1 << i)
            if i in groups:
                for j in groups[i]:
                    ndone |= 1 << j
            npast = past | {new} if (revert_mode and it.kind != "read") else past
            if dfs(pt[0], pt[1], new, ndone, npast, reverts):
                return True
        if revert_mode and reverts > 0:
            for v in sorted(past, key=str):
                if v != value and dfs(tb, tk + 1, v, done, past, reverts - 1):
                    return True
        return False

    ok = dfs(-INF, 0, h.initial, 0, frozenset({h.initial}), MAX_REVERTS if revert_mode else 0)
    dfs.cache_clear()
    if not ok and exhausted:
        return LinResult(ok=None, explored=explored)
    return LinResult(ok=ok, explored=explored)


def first_conflict(h: History, budget: int = 300_000) -> Item | None:
    """Earliest observation such that the history truncated there is non-linearizable."""
    obs = sorted(h.observations, key=lambda o: max(e for _, e in o.intervals))
    for k in range(len(obs)):
        cutoff = max(e for _, e in obs[k].intervals)
        sub = History(order_id=h.order_id, initial=h.initial, observations=obs[: k + 1])
        sub.effects = []
        for eff in h.effects:
            atts = [a for a in eff["attempts"] if a["window"][0] < cutoff]
            if atts:
                sub.effects.append({**eff, "attempts": atts})
        # Acked writes that completed after the cutoff are optional in the prefix.
        labels = {e["logical_id"] for e in sub.effects
                  if any(a["status"] == "ok" and a["window"][1] <= cutoff for a in e["attempts"])}
        for eff in sub.effects:
            if eff["logical_id"] not in labels:
                eff["attempts"] = [{**a, "status": "uncertain" if a["status"] == "ok" else a["status"]}
                                   for a in eff["attempts"]]
        res = check(sub, budget=budget, mandatory_labels=labels)
        if res.ok is False:
            return obs[k]
    return None
