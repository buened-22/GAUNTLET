"""Audit: can SEQUENTIAL, FAULT-FREE schedules expose each mutant?

Uses the unmodified engine and oracle with hand-written directive lists that contain
no run_until/crash/lose_response/interleave. Each schedule is run against the mutant
and against its base reference (control). Read-only with respect to the repository.
"""
import copy
import json

import psycopg

from gauntlet.actor.proxy import ProcessPool
from gauntlet.engine.controller import Controller
from gauntlet.impls.registry import get_info
from gauntlet.oracle.checker import RunOracle, verdict
from gauntlet.pg.provision import apply_legacy_schema, create_run_database, drop_run_database

INITIAL = {1: "100.00", 2: "200.00", 3: "300.00", 4: "400.00", 5: "500.00", 6: "600.00"}
FORBIDDEN = {"run_until", "crash", "lose_response", "interleave", "restart"}


class S:
    def __init__(self):
        self.d, self.ops, self.n = [], {}, 0

    def spawn(self, a, r):
        self.d.append({"kind": "spawn", "actor": a, "role": r}); self.roles = getattr(self, "roles", {}); self.roles[a] = r

    def op(self, a, op, key=None, **args):
        self.n += 1
        lid = f"Q{self.n:03d}"
        if op in ("create_order", "set_total", "add_charge"):
            args["key"] = key or f"k-{lid}"
        self.ops[lid] = {"op": op, "args": args, "role": self.roles[a]}
        self.d += [{"kind": "call", "actor": a, "logical_id": lid}, {"kind": "run", "actor": a}]
        return lid

    def until_done(self, a, bs=2):
        self.d.append({"kind": "backfill_until_done", "actor": a, "batch_size": bs, "max_batches": 20})

    def drain(self, a):
        self.d.append({"kind": "drain", "actor": a})

    def snap(self, label):
        self.d.append({"kind": "snapshot", "label": label})


def run(impl, s):
    assert not FORBIDDEN & {x["kind"] for x in s.d}, "schedule must be sequential and fault-free"
    db = create_run_database(prefix="gauntlet_audit")
    try:
        apply_legacy_schema(db.dsn, INITIAL)
        with psycopg.connect(db.dsn, autocommit=True) as mon:
            ctl = Controller(impl, db.dsn, mon, copy.deepcopy(s.ops), ProcessPool(3))
            try:
                ctl.execute(s.d)
                ctl.settle()
            finally:
                ctl.shutdown()
            res = RunOracle(strategy=get_info(impl).strategy, initial_orders=INITIAL, attempts=ctl.ledger.attempts(),
                            snapshots=ctl.snapshots, repeats=ctl.repeats, target_stage="none",
                            inconclusive=ctl.inconclusive).evaluate()
            return verdict(res, None, ctl.inconclusive), [r["id"] for r in res if r["status"] == "fail"]
    finally:
        drop_run_database(db)


def m1_seq():  # every backfill/legacy-write ordering that a sequential test can produce
    s = S(); s.spawn("v1", "v1"); s.spawn("m", "migrator"); s.op("m", "expand")
    s.op("v1", "set_total", order_id=2, total="9.78")          # legacy write before backfill
    s.until_done("m")                                            # backfill runs alone, to completion
    s.op("v1", "set_total", order_id=3, total="1.23")           # legacy write after backfill
    s.snap("after"); return s


def m2_seq():  # finalized-stage charges, sequential, no crash; a replay with the same key too
    s = S(); s.spawn("v1", "v1"); s.spawn("m", "migrator"); s.op("m", "expand"); s.until_done("m")
    s.drain("v1"); s.op("m", "finalize"); s.spawn("v2", "v2")
    s.op("v2", "add_charge", order_id=5, amount="8.29", key="dup")
    s.op("v2", "add_charge", order_id=5, amount="8.29", key="dup")  # sequential replay
    s.op("v2", "get_order", order_id=5); s.snap("after"); return s


def m3_seq():  # v2 duplicate-key replay, sequential (analogue of CT03, but on v2)
    s = S(); s.spawn("m", "migrator"); s.op("m", "expand"); s.spawn("v2", "v2")
    s.op("v2", "add_charge", order_id=1, amount="6.72", key="dup")
    s.op("v2", "add_charge", order_id=1, amount="6.72", key="dup")
    s.op("v2", "get_order", order_id=1); s.snap("after"); return s


def m7_seq():  # v2 charge on a not-yet-backfilled legacy row, sequential
    s = S(); s.spawn("m", "migrator"); s.op("m", "expand"); s.spawn("v2", "v2")
    s.op("v2", "add_charge", order_id=2, amount="36.98")
    s.op("v2", "get_order", order_id=2); s.snap("after"); return s


def m8_seq():  # legacy write after backfill, then drain/finalize, sequential
    s = S(); s.spawn("v1", "v1"); s.spawn("m", "migrator"); s.op("m", "expand"); s.until_done("m")
    s.op("v1", "set_total", order_id=2, total="438.53")
    s.drain("v1"); s.op("m", "finalize"); s.spawn("v2", "v2")
    s.op("v2", "get_order", order_id=2); s.snap("after"); return s


CASES = [("m1_stale_backfill", m1_seq), ("m2_nonatomic_dual_write", m2_seq), ("m3_retry_duplicates", m3_seq),
         ("m7_assumes_backfilled", m7_seq), ("m8_skip_reconcile", m8_seq)]

if __name__ == "__main__":
    for mutant, build in CASES:
        base = get_info(mutant).base
        rv, rf = run(base, build())
        mv, mf = run(mutant, build())
        print(json.dumps({"schedule": build.__name__, "reference": base, "reference_verdict": rv, "reference_fails": rf,
                          "mutant": mutant, "mutant_verdict": mv, "mutant_fails": mf}))
