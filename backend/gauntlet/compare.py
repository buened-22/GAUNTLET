"""Comparison of two completed runs."""

from __future__ import annotations

from collections import Counter

from gauntlet.engine.runner import logical_trace


def compare_runs(a: dict, b: dict) -> dict:
    la, lb = logical_trace(a.get("events", [])), logical_trace(b.get("events", []))
    divergence = None
    for i in range(min(len(la), len(lb))):
        if la[i] != lb[i]:
            divergence = {"index": i, "a": la[i], "b": lb[i]}
            break
    if divergence is None and len(la) != len(lb):
        i = min(len(la), len(lb))
        divergence = {"index": i, "a": la[i] if i < len(la) else None, "b": lb[i] if i < len(lb) else None}

    inv_a = {r["id"]: r for r in a.get("invariants", [])}
    inv_b = {r["id"]: r for r in b.get("invariants", [])}
    invariants = [{"id": k, "a": inv_a.get(k, {}).get("status"), "b": inv_b.get(k, {}).get("status"),
                   "differs": inv_a.get(k, {}).get("status") != inv_b.get(k, {}).get("status")}
                  for k in list(inv_a) + [k for k in inv_b if k not in inv_a]]

    def by_attempt(run):
        return {x["attempt_id"]: x for x in run.get("attempts", [])}

    at_a, at_b = by_attempt(a), by_attempt(b)
    outcome_diffs = []
    for aid in sorted(set(at_a) | set(at_b)):
        xa, xb = at_a.get(aid), at_b.get(aid)
        sa = None if xa is None else (xa["status"], xa.get("result"), (xa.get("error") or {}).get("code"))
        sb = None if xb is None else (xb["status"], xb.get("result"), (xb.get("error") or {}).get("code"))
        if sa != sb:
            outcome_diffs.append({"attempt_id": aid, "op": (xa or xb)["op"], "actor": (xa or xb)["actor"],
                                  "a": None if xa is None else {"status": xa["status"], "result": xa.get("result"),
                                                                "error": xa.get("error")},
                                  "b": None if xb is None else {"status": xb["status"], "result": xb.get("result"),
                                                                "error": xb.get("error")}})

    def final_rows(run):
        snaps = run.get("snapshots") or []
        return {r["id"]: r for r in snaps[-1]["rows"]} if snaps else {}

    fa, fb = final_rows(a), final_rows(b)
    final_diffs = [{"id": i, "a": fa.get(i), "b": fb.get(i)} for i in sorted(set(fa) | set(fb)) if fa.get(i) != fb.get(i)]

    def side(run):
        return {k: run.get(k) for k in ("id", "scenario_id", "impl_id", "impl_kind", "strategy", "seed", "verdict",
                                        "schedule_hash", "trace_hash", "outcome_hash", "duration_ms", "metrics",
                                        "created_at", "benchmark_version", "schedule_source")}

    return {
        "a": side(a), "b": side(b),
        "same_scenario": a.get("scenario_id") == b.get("scenario_id"),
        "same_implementation": a.get("impl_id") == b.get("impl_id"),
        "same_benchmark_version": a.get("benchmark_version") == b.get("benchmark_version"),
        "same_seed": a.get("seed") == b.get("seed"),
        "same_schedule": a.get("schedule_hash") == b.get("schedule_hash"),
        "same_trace": a.get("trace_hash") == b.get("trace_hash"),
        "same_outcome": a.get("outcome_hash") == b.get("outcome_hash"),
        "event_kinds": {"a": dict(Counter(e["kind"] for e in a.get("events", []))),
                        "b": dict(Counter(e["kind"] for e in b.get("events", [])))},
        "first_divergence": divergence,
        "invariants": invariants,
        "outcome_differences": outcome_diffs[:200],
        "outcome_differences_total": len(outcome_diffs),
        "final_state_differences": final_diffs,
    }
