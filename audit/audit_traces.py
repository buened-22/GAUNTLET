"""Audit: print the trace excerpt that exposes each mutant (read-only, uses stored runs)."""
import json
import sqlite3
import sys

c = sqlite3.connect(sys.argv[1])
s = json.loads(c.execute("select summary_json from benchmarks").fetchone()[0])
want = {"m1_stale_backfill": "S02", "m2_nonatomic_dual_write": "S03", "m3_retry_duplicates": "S03",
        "m7_assumes_backfilled": "S06", "m8_skip_reconcile": "S01"}


def fmt(e):
    k = e["kind"]
    if k in ("stmt", "unblocked"):
        return f"{e['cls']}: {e['sql'][:95]} params={json.dumps(e['params'])[:70]}"
    if k == "invoke":
        return f"INVOKE {e['attempt_id']} {e['op']} {json.dumps(e['args'])}{' RETRY' if e.get('retry') else ''}"
    if k == "respond":
        return f"RESPOND {e['attempt_id']} {'delivered' if e['delivered'] else 'LOST'} {json.dumps(e['outcome'])[:90]}"
    if k == "snapshot":
        return f"SNAPSHOT '{e['label']}'"
    return f"{k.upper()} " + json.dumps({x: e[x] for x in e if x in ("label", "cond", "fault", "attempt_id", "next_sql")})[:120]


def mentions(params, oid):
    vals = params if isinstance(params, list) else list(params.values()) if isinstance(params, dict) else []
    return any(v == oid or (isinstance(v, list) and oid in v) for v in vals)


for impl, sc in want.items():
    rid = next(r["id"] for r in s["runs"] if r["impl_id"] == impl and r["scenario_id"].startswith(sc) and r["seed"] == 1)
    d = json.loads(c.execute("select data_json from runs where id=?", (rid,)).fetchone()[0])
    s7 = next(i for i in d["invariants"] if i["id"] == "S7_LINEARIZABLE")
    ev0 = s7["evidence"][0]
    oid, trig = ev0["order_id"], ev0["first_conflict"]["seq"]
    print(f"\n######## {impl} x {d['scenario_id']} seed 1 run {rid}")
    print(f"order {oid}: initial={ev0['initial']} writes={json.dumps(ev0['writes'])[:420]}")
    print(f"first conflict: {ev0['first_conflict']}  explained_by={ev0['explained_by']}")
    lids = {w["logical_id"] for w in ev0["writes"]}
    rel = {a["attempt_id"] for a in d["attempts"] if a["logical_id"] in lids}
    for e in d["events"]:
        if e["seq"] > trig:
            break
        hit = e.get("attempt_id") in rel or e["kind"] in ("crash", "paused", "restart", "stage") or e["seq"] == trig \
            or (e["kind"] in ("stmt", "unblocked") and e.get("cls") in ("write", "ddl") and mentions(e.get("params"), oid)) \
            or (e["kind"] == "invoke" and e.get("op") in ("finalize", "backfill_batch", "expand"))
        if hit:
            print(f"  #{e['seq']:<4} {str(e['actor']):<9} {fmt(e)}")
    for snap in d["snapshots"]:
        if snap["seq"] == trig:
            print("  snapshot row:", [r for r in snap["rows"] if r["id"] == oid])
