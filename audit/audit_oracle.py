"""Audit of the oracle on hand-constructed histories (no database, no implementation code).

For each case: the history, the outcome the exact-cents linearizable model SHOULD give,
and what RunOracle actually returns. Mismatches are printed, not hidden.

Timeline convention: an attempt invoked at seq i and completed/crashed/lost at seq c may
take effect only in (i, c). A snapshot at seq q observes the state at q.
"""
from gauntlet.oracle.checker import RunOracle

INIT = {1: "10.00"}


def att(i, c, aid, lid, op, args, status="ok", result=None, error=None, reason="delivered", role="v2"):
    return {"seq": i, "attempt_id": aid, "logical_id": lid, "actor": "a", "role": role, "op": op, "args": args,
            "registered": True, "invoke_seq": i, "complete_seq": c, "status": status, "reason": reason,
            "result": result, "error": error}


def snap(q, total, label="s"):
    return {"label": label, "seq": q, "columns": ["id", "total"], "rows": [{"id": 1, "total": total}],
            "not_null": {}, "triggers": []}


def SET(i, c, aid, lid, v, **kw):
    return att(i, c, aid, lid, "set_total", {"order_id": 1, "total": v, "key": f"k-{lid}"}, **kw)


def ADD(i, c, aid, lid, v, out=None, **kw):
    return att(i, c, aid, lid, "add_charge", {"order_id": 1, "amount": v, "key": f"k-{lid}"},
               result={"total": out} if out else None, **kw)


def GET(i, c, aid, v):
    return att(i, c, aid, aid, "get_order", {"order_id": 1}, result={"total": v})


CASES = [
    # --- lost writes ---------------------------------------------------------------
    ("LW1 acked set never visible", [SET(1, 2, "A.a1", "A", "5.00")], [snap(10, "10.00")],
     "FAIL S7; S1 FAIL (lost ack)"),
    ("LW2 acked set visible, then reverted (stale overwrite)",
     [SET(1, 2, "A.a1", "A", "5.00"), GET(3, 4, "R", "5.00")], [snap(10, "10.00")],
     "FAIL S7; S1 FAIL"),
    ("LW3 acked set persists", [SET(1, 2, "A.a1", "A", "5.00")], [snap(10, "5.00")], "PASS"),
    # --- duplicates ----------------------------------------------------------------
    ("DU1 lost response + retry, applied once",
     [ADD(1, 2, "A.a1", "A", "1.00", status="uncertain", reason="response_lost"), ADD(3, 4, "A.a2", "A", "1.00", out="11.00")],
     [snap(10, "11.00")], "PASS"),
    ("DU2 lost response + retry, applied twice",
     [ADD(1, 2, "A.a1", "A", "1.00", status="uncertain", reason="response_lost"), ADD(3, 4, "A.a2", "A", "1.00", out="12.00")],
     [snap(10, "12.00")], "FAIL S7; S4 FAIL"),
    ("DU3 same key reused by a DIFFERENT logical id, impl applies once (correct idempotency)",
     [ADD(1, 2, "A.a1", "A", "1.00", out="11.00"),
      att(3, 4, "B.a1", "B", "add_charge", {"order_id": 1, "amount": "1.00", "key": "k-A"}, result={"total": "11.00"})],
     [snap(10, "11.00")], "SHOULD PASS if logical identity = idempotency key (as docs/INVARIANTS.md states)"),
    # --- writes acknowledged before a crash ------------------------------------------
    ("AC1 acked set, process crashes later (later op uncertain); acked effect lost",
     [SET(1, 2, "A.a1", "A", "5.00"), SET(3, 4, "B.a1", "B", "6.00", status="uncertain", reason="crashed")],
     [snap(10, "10.00")], "FAIL (A was acknowledged; neither 5.00 nor 6.00 observed)"),
    ("AC2 acked set, later crashed set did not apply", [SET(1, 2, "A.a1", "A", "5.00"),
     SET(3, 4, "B.a1", "B", "6.00", status="uncertain", reason="crashed")], [snap(10, "5.00")], "PASS"),
    ("AC3 acked set, later crashed set did apply", [SET(1, 2, "A.a1", "A", "5.00"),
     SET(3, 4, "B.a1", "B", "6.00", status="uncertain", reason="crashed")], [snap(10, "6.00")], "PASS"),
    # --- uncertain outcomes -----------------------------------------------------------
    ("UN1 crashed after commit, never retried, applied", [ADD(1, 2, "A.a1", "A", "1.00", status="uncertain", reason="crashed")],
     [snap(10, "11.00")], "PASS"),
    ("UN2 crashed, never retried, not applied", [ADD(1, 2, "A.a1", "A", "1.00", status="uncertain", reason="crashed")],
     [snap(10, "10.00")], "PASS"),
    ("UN3 crashed, value matches neither", [ADD(1, 2, "A.a1", "A", "1.00", status="uncertain", reason="crashed")],
     [snap(10, "10.50")], "FAIL"),
    ("UN4 crashed then retried OK, but effect absent", [ADD(1, 2, "A.a1", "A", "1.00", status="uncertain", reason="crashed"),
     ADD(3, 4, "A.a2", "A", "1.00", out="11.00")], [snap(10, "10.00")], "FAIL (an ok attempt makes it mandatory)"),
    ("UN5 effect appears only AFTER the crash point (window closed at seq 5)",
     [ADD(1, 5, "A.a1", "A", "1.00", status="uncertain", reason="crashed")], [snap(7, "10.00"), snap(9, "11.00")],
     "FAIL (a dead process cannot commit later)"),
    ("UN6 uncertain effect observed, then disappears",
     [ADD(1, 2, "A.a1", "A", "1.00", status="uncertain", reason="crashed")], [snap(5, "11.00"), snap(9, "10.00")],
     "FAIL (once applied it cannot be un-applied)"),
    ("UN7 effect lands in the GAP between attempts (after a1 crashed, before a2 invoked)",
     [ADD(1, 4, "A.a1", "A", "1.00", status="uncertain", reason="crashed"), ADD(10, 12, "A.a2", "A", "1.00", out="11.00")],
     [snap(6, "10.00"), snap(8, "11.00")], "FAIL (no attempt is executing between 4 and 10)"),
    ("UN8 retry returns stored response but effect placed in retry window only",
     [ADD(1, 4, "A.a1", "A", "1.00", status="uncertain", reason="response_lost"), ADD(10, 12, "A.a2", "A", "1.00", out="11.00")],
     [snap(6, "10.00"), snap(14, "11.00")], "PASS (effect may legitimately happen during a2)"),
    # --- failed operations -------------------------------------------------------------
    ("FA1 explicit failure that actually applied", [SET(1, 2, "A.a1", "A", "5.00", status="failed",
     error={"kind": "app", "code": "x"})], [snap(10, "5.00")], "FAIL S7; S6 FAIL"),
    # --- concurrency -------------------------------------------------------------------
    ("CO1 two overlapping acked sets; either final order allowed (5.00)",
     [SET(1, 6, "A.a1", "A", "5.00"), SET(2, 7, "B.a1", "B", "6.00")], [snap(10, "5.00")], "PASS"),
    ("CO2 sequential acked sets; earlier value final", [SET(1, 2, "A.a1", "A", "5.00"), SET(3, 4, "B.a1", "B", "6.00")],
     [snap(10, "5.00")], "FAIL (B strictly after A)"),
]

if __name__ == "__main__":
    print(f"{'case':<88} {'S7':<6} {'S1':<13} {'S4':<13} {'S6':<13} expected")
    for name, atts, snaps, expected in CASES:
        res = {r["id"]: r for r in RunOracle(strategy="A", initial_orders=INIT, attempts=atts, snapshots=snaps,
                                              repeats=[], target_stage="none").evaluate()}
        g = lambda k: res[k]["status"]  # noqa: E731
        print(f"{name:<88} {g('S7_LINEARIZABLE'):<6} {g('S1_ACKED_DURABLE'):<13} {g('S4_NO_DUPLICATION'):<13} "
              f"{g('S6_FAILED_NOT_APPLIED'):<13} {expected}")
