"""Audit: sequential, fault-free replays that reuse the SAME logical operation (engine retry path).

The first response is delivered normally; the client then resubmits the same logical
operation (same idempotency key). No crash, no dropped response, no interleaving.
"""
import json

from audit_sequential import S, run
from gauntlet.impls.registry import get_info


def retry(s, a, lid):
    s.d += [{"kind": "call", "actor": a, "logical_id": lid, "retry": True}, {"kind": "run", "actor": a}]


def m2_seq_retry():
    s = S(); s.spawn("v1", "v1"); s.spawn("m", "migrator"); s.op("m", "expand"); s.until_done("m")
    s.drain("v1"); s.op("m", "finalize"); s.spawn("v2", "v2")
    lid = s.op("v2", "add_charge", order_id=5, amount="8.29"); retry(s, "v2", lid)
    s.op("v2", "get_order", order_id=5); s.snap("after"); return s


def m3_seq_retry():
    s = S(); s.spawn("m", "migrator"); s.op("m", "expand"); s.spawn("v2", "v2")
    lid = s.op("v2", "add_charge", order_id=1, amount="6.72"); retry(s, "v2", lid)
    s.op("v2", "get_order", order_id=1); s.snap("after"); return s


if __name__ == "__main__":
    for mutant, build in [("m2_nonatomic_dual_write", m2_seq_retry), ("m3_retry_duplicates", m3_seq_retry)]:
        base = get_info(mutant).base
        rv, rf = run(base, build())
        mv, mf = run(mutant, build())
        print(json.dumps({"schedule": build.__name__, "reference": base, "reference_verdict": rv, "reference_fails": rf,
                          "mutant": mutant, "mutant_verdict": mv, "mutant_fails": mf}))
