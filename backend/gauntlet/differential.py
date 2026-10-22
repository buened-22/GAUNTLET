"""Differential testing: run two or more implementations on the same logical schedule and compare.

Protocol
--------
1. One schedule document is fixed up front (catalog scenario + seed, a fixture, or an explicit
   schedule copied from a persisted run). Its hash and initial-state fingerprint are recorded.
2. Each implementation executes that exact document on its own freshly created database (no state
   is reused between implementations). Runs are persisted like any other run.
3. Client-visible behaviour is normalized (rules below) and compared pairwise.
4. The independent oracle's verdicts (computed per run, from the ledger and raw snapshots, never
   from the other implementation) decide whether a disagreement is a *confirmed* violation.

Equivalent logical input is not equivalent physical execution: statement-level interleavings are
chosen per step among *runnable* actors, so two implementations that issue different statements
can legitimately experience different interleavings of the same workload. Such differences are
reported, not assumed to be bugs.

Classification (``CLASSIFICATIONS``)
------------------------------------
* ``inconclusive``: any run ended in an infrastructure error / timeout / limit / schedule error, or
  behaviour differs and no run has an invariant FAIL while at least one run is inconclusive.
* ``confirmed_violation``: normalized behaviour differs and the oracle FAILs at least one invariant
  in at least one run.
* ``unconfirmed_disagreement``: behaviour differs, every run passes the oracle.
* ``agreement``: no normalized difference. Agreement does not prove correctness: if the oracle
  FAILs invariants in every run (a shared defect), ``shared_violations`` lists them.
"""

from __future__ import annotations

from gauntlet.engine.runner import logical_trace
from gauntlet.execution import outcome_class, replay, run_and_save
from gauntlet.impls.registry import is_known
from gauntlet.schedules import build_schedule, initial_state_fingerprint, resolve_scenario, schedule_id
from gauntlet.store import Store

CLASSIFICATIONS = ("agreement", "confirmed_violation", "unconfirmed_disagreement", "inconclusive")
CLIENT_ROLES = ("v1", "v2")

NORMALIZATION_RULES = [
    {"id": "N1", "rule": "Observational trace fields (t_ms, backend_pid, blockers) are excluded.",
     "why": "Wall-clock offsets and OS process ids differ on every execution and carry no logical meaning."},
    {"id": "N2", "rule": "Errors are compared by their error code only, not their free-text message.",
     "why": "Messages are implementation-chosen prose; the code is the client-visible contract."},
    {"id": "N3", "rule": "Migration-operation results (expand / backfill_batch / finalize / contract) are compared "
                         "by status and error code only; their result payloads are not compared.",
     "why": "Payloads are implementation bookkeeping (e.g. rows updated per batch, rows reconciled) that the task "
            "does not specify; the effects of migration steps are judged through client results, final state and "
            "the oracle."},
    {"id": "N4", "rule": "Client operations (create_order / set_total / add_charge / get_order) are compared on "
                         "status (ok / failed / uncertain), full result and error code, per attempt id.",
     "why": "These are the acknowledgements and values clients observe; nothing is normalized away."},
    {"id": "N5", "rule": "Final state: the final snapshot's column list, NOT NULL flags, triggers, the reported "
                         "migration stage and every row (sorted by id) are compared exactly; a missing, extra or "
                         "different row, a lost constraint, a leftover trigger or a different stage is a difference.",
     "why": "Final state is part of the task contract; intermediate snapshots are not compared because their "
            "timing depends on the implementation's statement count."},
    {"id": "N6", "rule": "Attempts are matched by attempt id (logical id + attempt number); an attempt present for "
                         "one implementation only is a difference.",
     "why": "Missing or extra attempts (e.g. a retry that happened for only one side) are behaviour."},
]


def _err_code(e) -> str | None:
    return (e or {}).get("code") if isinstance(e, dict) else (str(e) if e else None)


def normalized_attempts(run: dict) -> dict[str, dict]:
    out = {}
    for a in run.get("attempts", []):
        client = a.get("role") in CLIENT_ROLES
        out[a["attempt_id"]] = {"op": a["op"], "actor": a["actor"], "role": a.get("role"),
                                "status": a["status"], "error_code": _err_code(a.get("error")),
                                "result": a.get("result") if client else None}
    return out


def normalized_final(run: dict) -> dict:
    snaps = run.get("snapshots") or []
    if not snaps:
        return {"columns": None, "rows": None, "meta": None}
    last = snaps[-1]
    meta = {"not_null": last.get("not_null"), "triggers": sorted(last.get("triggers") or []),
            "migration_stage": last.get("impl_reported_stage")}
    return {"columns": list(last.get("columns") or []), "rows": sorted(last.get("rows") or [], key=lambda r: r.get("id")),
            "meta": meta}


def _context(run: dict, seq: int | None, n: int = 8) -> list[dict]:
    """The ``n`` logical events up to and including ``seq`` (observational fields removed)."""
    if seq is None:
        return []
    ev = [e for e in logical_trace(run.get("events", [])) if e["seq"] <= seq]
    return ev[-n:]


def compare_pair(a: dict, b: dict) -> dict:
    na, nb = normalized_attempts(a), normalized_attempts(b)
    order = [x["attempt_id"] for x in a.get("attempts", [])] + [x["attempt_id"] for x in b.get("attempts", [])
                                                               if x["attempt_id"] not in na]
    differing = [aid for aid in order if na.get(aid) != nb.get(aid)]
    fa, fb = normalized_final(a), normalized_final(b)
    rows_a = {r.get("id"): r for r in fa["rows"] or []}
    rows_b = {r.get("id"): r for r in fb["rows"] or []}
    final_rows = [{"id": i, "a": rows_a.get(i), "b": rows_b.get(i)}
                  for i in sorted(set(rows_a) | set(rows_b), key=lambda x: (x is None, x)) if rows_a.get(i) != rows_b.get(i)]
    first = None
    if differing:
        aid = differing[0]
        att_a = next((x for x in a.get("attempts", []) if x["attempt_id"] == aid), None)
        att_b = next((x for x in b.get("attempts", []) if x["attempt_id"] == aid), None)
        first = {"attempt_id": aid, "a": na.get(aid), "b": nb.get(aid),
                 "a_complete_seq": (att_a or {}).get("complete_seq"), "b_complete_seq": (att_b or {}).get("complete_seq"),
                 "a_context": _context(a, (att_a or {}).get("complete_seq") or (att_a or {}).get("invoke_seq")),
                 "b_context": _context(b, (att_b or {}).get("complete_seq") or (att_b or {}).get("invoke_seq"))}
    la, lb = logical_trace(a.get("events", [])), logical_trace(b.get("events", []))
    trace_div = next((i for i in range(min(len(la), len(lb))) if la[i] != lb[i]),
                     None if len(la) == len(lb) else min(len(la), len(lb)))
    return {
        "a": a["id"], "b": b["id"],
        "differs": bool(differing or final_rows or fa["columns"] != fb["columns"] or fa["meta"] != fb["meta"]),
        "attempt_differences": [{"attempt_id": aid, "a": na.get(aid), "b": nb.get(aid)} for aid in differing][:200],
        "attempt_differences_total": len(differing),
        "final_columns": {"a": fa["columns"], "b": fb["columns"], "same": fa["columns"] == fb["columns"]},
        "final_metadata": {"a": fa["meta"], "b": fb["meta"], "same": fa["meta"] == fb["meta"]},
        "final_row_differences": final_rows,
        "first_meaningful_divergence": first,
        "first_statement_level_divergence_index": trace_div,
    }


def classify(runs: list[dict], pairs: list[dict]) -> dict:
    errored = [r for r in runs if r.get("infra_error")]
    # A run record without a verdict or invariant results was not (fully) captured: never a basis for agreement.
    partial = [r for r in runs if not r.get("infra_error") and (not r.get("invariants") or not r.get("verdict"))]
    fails = {r["impl_id"]: [i["id"] for i in (r.get("invariants") or []) if i.get("status") == "fail"] for r in runs}
    differs = any(p["differs"] for p in pairs)
    shared = None
    if errored or partial:
        cls = "inconclusive"
        reason = "; ".join([f"{r['impl_id']}: {outcome_class(r)} ({(r['infra_error'] or '')[:160]})" for r in errored]
                           + [f"{r['impl_id']}: incomplete run record (no verdict or invariant results)" for r in partial])
    elif differs and any(fails.values()):
        cls = "confirmed_violation"
        reason = "behaviour differs and the oracle FAILs: " + "; ".join(
            f"{k}: {', '.join(v)}" for k, v in fails.items() if v)
    elif differs and any(r["verdict"] == "inconclusive" for r in runs):
        cls = "inconclusive"
        reason = "behaviour differs, no invariant FAILs, and at least one run is inconclusive"
    elif differs:
        cls = "unconfirmed_disagreement"
        reason = ("normalized behaviour differs but the oracle establishes no invariant violation in any run; "
                  "the difference may be harmless")
    else:
        cls = "agreement"
        reason = "no normalized difference in client outcomes or final state"
        common = set.intersection(*(set(v) for v in fails.values())) if fails else set()
        if any(fails.values()):
            shared = {k: v for k, v in fails.items()}
            reason += ("; NOTE: the oracle FAILs invariants in these runs, so agreement here means the "
                       "implementations share the observed behaviour, not that it is correct")
        if common:
            shared = {**(shared or {}), "_common": sorted(common)}
    return {"classification": cls, "reason": reason, "shared_violations": shared,
            "oracle": {r["impl_id"]: {"run_id": r.get("id"), "verdict": r.get("verdict"),
                                      "outcome": outcome_class(r) if r.get("verdict") else "incomplete",
                                      "failed_invariants": fails[r["impl_id"]],
                                      "inconclusive_invariants": [i["id"] for i in (r.get("invariants") or [])
                                                                  if i.get("status") == "inconclusive"]}
                       for r in runs}}


def validate_request(impl_ids: list[str], scenario_id: str) -> list[str]:
    problems = []
    if len(impl_ids) < 2:
        problems.append("at least two implementations are required")
    if len(set(impl_ids)) != len(impl_ids):
        problems.append("implementations must be distinct")
    for i in impl_ids:
        if not is_known(i):
            problems.append(f"unknown implementation {i!r}")
    try:
        resolve_scenario(scenario_id)
    except KeyError as exc:
        problems.append(str(exc))
    # Every registered implementation implements the full migration contract (impls/base.py), and every
    # scenario only uses contract operations, so any known implementation is compatible with any scenario.
    return problems


def _summary_of(run: dict) -> dict:
    return {k: run.get(k) for k in ("id", "impl_id", "impl_kind", "strategy", "verdict", "schedule_hash", "trace_hash",
                                    "outcome_hash", "duration_ms", "database", "error_class", "infra_error")} | {
        "provenance": run.get("provenance"),
        "failed_invariants": [i["id"] for i in run["invariants"] if i["status"] == "fail"]}


def run_differential(store: Store | None, impl_ids: list[str], scenario_id: str, seed: int, *,
                     schedule: dict | None = None, time_limit_s: float | None = 180,
                     comparison_id: str | None = None, replay_of: str | None = None,
                     isolation: dict | None = None) -> dict:
    problems = validate_request(impl_ids, scenario_id)
    if problems:
        raise ValueError("; ".join(problems))
    sched = schedule or build_schedule(scenario_id, seed)
    config = {"impl_ids": impl_ids, "scenario_id": scenario_id, "seed": seed,
              "schedule": sched, "schedule_id": schedule_id(sched),
              "schedule_source": "explicit" if schedule is not None else "scenario",
              "initial_state_fingerprint": initial_state_fingerprint(sched["initial_orders"]),
              "time_limit_s": time_limit_s, "replay_of": replay_of, "isolation": isolation,
              "normalization_rules": [r["id"] for r in NORMALIZATION_RULES]}
    cid = comparison_id
    if store is not None and cid is None:
        cid = store.create_analysis("differential", config, status="running")
    elif store is not None:
        store.update_analysis(cid, status="running", config=config)
    runs = []
    for impl in impl_ids:
        runs.append(run_and_save(store, scenario_id, impl, seed, schedule=schedule,
                                 origin={"kind": "differential", "id": cid}, time_limit_s=time_limit_s,
                                 isolation=isolation))  # applies to external candidates only
    pairs = [compare_pair(runs[i], runs[j]) for i in range(len(runs)) for j in range(i + 1, len(runs))]
    result = {
        **classify(runs, pairs),
        "runs": [_summary_of(r) for r in runs],
        "pairs": pairs,
        "equivalence": {
            "same_schedule_hash": len({r["schedule_hash"] for r in runs}) == 1,
            "same_initial_state_fingerprint": len({r["provenance"]["initial_state_fingerprint"] for r in runs}) == 1,
            "isolated_databases": len({r["database"] for r in runs if r["database"]}) == len([r for r in runs if r["database"]]),
            "databases": {r["impl_id"]: r["database"] for r in runs},
        },
        "normalization_rules": NORMALIZATION_RULES,
    }
    if store is not None:
        store.update_analysis(cid, status="completed", result=result)
    return {"id": cid, "config": config, "result": result}


def replay_differential(store: Store, comparison_id: str) -> dict:
    """Re-run a persisted comparison's exact schedule and compare per-implementation hashes and classification."""
    orig = store.get_analysis(comparison_id)
    if orig is None or orig["kind"] != "differential":
        raise KeyError(f"no differential comparison {comparison_id}")
    cfg = orig["config"]
    again = run_differential(store, cfg["impl_ids"], cfg["scenario_id"], cfg["seed"],
                             schedule=cfg["schedule"] if cfg.get("schedule_source") == "explicit" else None,
                             time_limit_s=cfg.get("time_limit_s"), replay_of=comparison_id,
                             isolation=cfg.get("isolation"))
    before = {r["impl_id"]: r for r in orig["result"]["runs"]}
    after = {r["impl_id"]: r for r in again["result"]["runs"]}
    per_impl = {i: {h: before[i][h] == after[i][h] for h in ("schedule_hash", "trace_hash", "outcome_hash", "verdict")}
                for i in cfg["impl_ids"]}
    summary = {"replay_of": comparison_id, "replay_id": again["id"], "per_implementation": per_impl,
               "same_classification": orig["result"]["classification"] == again["result"]["classification"],
               "reproduced": all(all(v.values()) for v in per_impl.values())
               and orig["result"]["classification"] == again["result"]["classification"]}
    res = store.get_analysis(again["id"])["result"]
    res["replay_check"] = summary
    store.update_analysis(again["id"], result=res)
    return summary


__all__ = ["CLASSIFICATIONS", "NORMALIZATION_RULES", "classify", "compare_pair", "replay_differential",
           "run_differential", "replay"]
