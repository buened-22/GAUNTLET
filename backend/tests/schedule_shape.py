"""Amount-independent structural signature of a schedule definition (shared by tests).

Two schedules have the same shape when they issue the same directives, on the same actors, with the
same fault positions, operations and target orders, in the same order. Monetary amounts, idempotency
keys and the numeric seeds handed to ``interleave`` directives are excluded, so a seed that only
changes input amounts does NOT count as a different schedule.
"""

from __future__ import annotations


def _op_shape(ops: dict, lid: str) -> tuple:
    o = ops[lid]
    return (o["op"], o["args"].get("order_id"), o["args"].get("batch_size"))


def schedule_shape(sched: dict) -> tuple:
    ops = sched["ops"]
    shape = []
    for d in sched["directives"]:
        item = [d["kind"], d.get("actor"), d.get("cond"), d.get("n"), d.get("fault"), d.get("retry"),
                d.get("label"), d.get("role"), d.get("batch_size")]
        if d.get("logical_id") in ops:
            item.append(_op_shape(ops, d["logical_id"]))
        if d["kind"] == "interleave":
            item.append(tuple((a, tuple(_op_shape(ops, lid) for lid in q)) for a, q in sorted(d["queues"].items())))
        shape.append(tuple(item))
    return tuple(shape)
