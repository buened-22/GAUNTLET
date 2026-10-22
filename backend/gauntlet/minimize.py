"""Automatic failure minimization: delta debugging (ddmin) over removable schedule units.

Input: a persisted run with a confirmed invariant violation. The original run is never modified;
every candidate is a new explicit schedule executed on its own fresh database and persisted as a
new run.

Units
-----
* ``op:<lid>``: a logical operation's directive group (its ``call``, the ``run`` / ``run_until`` /
  ``lose_response`` directives that drive it, retries of it, ``retry_until_ok`` of it);
* ``q:<i>:<lid>``: one operation queued in interleave directive ``i`` (plus, for an interleave with
  explicit choices, the choice entries that stepped that operation, taken from the run's decisions);
* ``snapshot:<i>``, ``backfill:<i>``: single directives;
* ``repeat:<i>``: a ``repeat`` of an operation; it depends on that operation's unit and is removed
  whenever that unit is removed.
``spawn``, ``drain``, ``crash``, ``restart`` and ``stage`` directives are structural and always kept.

A candidate is executed only if it passes static validation; an execution that ends in an
infrastructure error, timeout or schedule error never counts as reproducing the failure.

Failure identity
----------------
The *failure signature* of a run for target invariant T is: T has status FAIL, the set of oracle
diagnosis categories (``explained_by``) in T's evidence, and the set of affected order ids. A
candidate reproduces the failure only if T FAILs, its categories are a non-empty subset of the
original's (when the original has any), and (policy ``"intersect"``, default) it affects at least one
of the originally affected orders. A different violated invariant alone is an unrelated failure.

Result
------
The reduction is *1-minimal with respect to unit deletion* only when the final pass tried removing
every single remaining unit and each removal was rejected on its evidence (a removal whose execution
ended in an infrastructure error or timeout, or was cut off by the budget, was not judged and voids the
claim); no global minimality is claimed. Other
simplifications (smaller batch sizes, fewer rows) are not attempted.
"""

from __future__ import annotations

import copy
import time
from typing import Callable

from gauntlet.execution import outcome_class, run_and_save
from gauntlet.schedules import derive_schedule, schedule_id, validate_schedule
from gauntlet.store import Store

DEFAULT_BUDGET = {
    "max_attempts": 60,          # candidate executions (excluding the reproduction and final replays)
    "max_duration_s": 3600,
    "run_time_limit_s": 120,
    "reproduce_repeats": 1,      # executions of the original schedule required to reproduce before reducing
    "confirm_repeats": 1,        # executions a candidate must reproduce in to be accepted
    "max_schedule_directives": 1000,
    "orders_policy": "intersect",  # or "any"
}
STRUCTURAL = ("spawn", "drain", "crash", "restart", "stage")


# -- units ------------------------------------------------------------------------------------------
def extract_units(schedule: dict) -> list[dict]:
    units: dict[str, dict] = {}
    order: list[str] = []
    cur: dict[str, str] = {}

    def unit(uid, kind, desc, deps=()):
        if uid not in units:
            units[uid] = {"id": uid, "kind": kind, "desc": desc, "directives": [], "queue": [], "deps": list(deps)}
            order.append(uid)
        return units[uid]

    ops = schedule["ops"]
    for i, d in enumerate(schedule["directives"]):
        k, actor = d["kind"], d.get("actor")
        if k in STRUCTURAL:
            continue
        if k == "call":
            lid = d["logical_id"]
            u = unit(f"op:{lid}", "op", f"{actor} {ops[lid]['op']} {lid} {ops[lid]['args']}")
            u["directives"].append(i)
            cur[actor] = lid
        elif k in ("run", "run_until", "lose_response"):
            if actor in cur:
                units[f"op:{cur[actor]}"]["directives"].append(i)
        elif k == "retry_until_ok":
            lid = d["logical_id"]
            unit(f"op:{lid}", "op", f"{actor} {ops[lid]['op']} {lid}")["directives"].append(i)
        elif k == "repeat":
            lid = d["logical_id"]
            unit(f"repeat:{i}", "repeat", f"repeat of {lid} ({ops[lid]['op']})", deps=[f"op:{lid}"])["directives"].append(i)
        elif k == "snapshot":
            unit(f"snapshot:{i}", "snapshot", f"snapshot {d.get('label')}")["directives"].append(i)
        elif k == "backfill_until_done":
            unit(f"backfill:{i}", "backfill", f"backfill_until_done batch_size={d.get('batch_size')}")["directives"].append(i)
        elif k == "interleave":
            for a, q in d["queues"].items():
                for lid in q:
                    unit(f"q:{i}:{lid}", "queued", f"interleave {i}: {a} {ops[lid]['op']} {lid} {ops[lid]['args']}"
                         )["queue"].append((i, a, lid))
    return [units[u] for u in order]


def _closure_removed(units: list[dict], removed: set[str]) -> set[str]:
    removed = set(removed)
    changed = True
    while changed:
        changed = False
        for u in units:
            if u["id"] not in removed and any(dep in removed for dep in u["deps"]):
                removed.add(u["id"])
                changed = True
    return removed


def build_candidate(original: dict, decisions: dict, units: list[dict], kept: list[str], *, derived_from: dict) -> dict:
    """Schedule with only the ``kept`` units (plus structural directives); dependents of removed units
    are removed too. Explicit interleave choices lose the entries of removed operations and become lenient."""
    removed = _closure_removed(units, {u["id"] for u in units} - set(kept))
    drop_directives = {i for u in units if u["id"] in removed for i in u["directives"]}
    drop_queued = {(i, a, lid) for u in units if u["id"] in removed for (i, a, lid) in u["queue"]}
    removed_lids_by_il: dict[int, set[str]] = {}
    for (i, _a, lid) in drop_queued:
        removed_lids_by_il.setdefault(i, set()).add(lid)
    directives = []
    for i, d in enumerate(original["directives"]):
        if i in drop_directives:
            continue
        d = copy.deepcopy(d)
        if d["kind"] == "interleave":
            d["queues"] = {a: [lid for lid in q if (i, a, lid) not in drop_queued] for a, q in d["queues"].items()}
            if d.get("choices") is not None and removed_lids_by_il.get(i):
                log = decisions.get(str(i), [])
                gone = removed_lids_by_il[i]
                d["choices"] = [e["chosen"] for e in log if e.get("logical_id") not in gone]
                d["choice_mode"] = "lenient"
        directives.append(d)
    used = {d["logical_id"] for d in directives if d.get("logical_id")}
    used |= {lid for d in directives if d["kind"] == "interleave" for q in d["queues"].values() for lid in q}
    ops = {k: v for k, v in original["ops"].items() if k in used}
    return derive_schedule(original, directives=directives, ops=ops, derived_from=derived_from)


def strictify(schedule: dict, run: dict) -> dict:
    """Replace lenient/partial interleave choices by the choices the run actually took (exact replay)."""
    out = copy.deepcopy(schedule)
    for i, d in enumerate(out["directives"]):
        if d["kind"] == "interleave" and d.get("choices") is not None:
            d["choices"] = [e["chosen"] for e in run.get("decisions", {}).get(str(i), [])]
            d["choice_mode"] = "strict"
    return out


# -- signature --------------------------------------------------------------------------------------
def signature(run: dict, invariant: str) -> dict | None:
    if run.get("infra_error"):
        return None
    inv = next((i for i in run.get("invariants", []) if i["id"] == invariant), None)
    if inv is None or inv["status"] != "fail":
        return None
    ev = [e for e in (inv.get("evidence") or []) if isinstance(e, dict)]
    orders = {e["order_id"] for e in ev if "order_id" in e}
    orders |= {r["id"] for e in ev for r in (e.get("rows") or []) if isinstance(r, dict) and "id" in r}
    return {"invariant": invariant, "status": "fail", "category": inv.get("category"),
            "diagnosis_categories": sorted({c for e in ev for c in (e.get("explained_by") or [])}),
            "orders": sorted(orders),
            "ops": sorted({e["op"] for e in ev if isinstance(e.get("op"), str)}),
            "error_codes": sorted({e["error"]["code"] for e in ev
                                   if isinstance(e.get("error"), dict) and e["error"].get("code")})}


def matches(target: dict, cand: dict | None, policy: str = "intersect") -> tuple[bool, str]:
    if cand is None:
        return False, f"{target['invariant']} does not FAIL"
    if cand["invariant"] != target["invariant"]:
        return False, "different invariant"
    tc, cc = set(target["diagnosis_categories"]), set(cand["diagnosis_categories"])
    if tc and (not cc or not cc <= tc):
        return False, f"diagnosis categories {sorted(cc)} not a non-empty subset of {sorted(tc)}"
    if policy == "intersect":
        # Every identifying feature the original evidence has must overlap: affected orders / rows,
        # failing operations, error codes (older signatures without ops / error_codes match on orders only).
        for key, what in (("orders", "affected orders"), ("ops", "failing operations"), ("error_codes", "error codes")):
            want = set(target.get(key) or [])
            if want and not want & set(cand.get(key) or []):
                return False, f"{what} {sorted(cand.get(key) or [])} disjoint from original {sorted(want)}"
    return True, "same failure signature"


def size_of(schedule: dict, run: dict | None = None) -> dict:
    return {"directives": len(schedule["directives"]),
            "operations_defined": len(schedule["ops"]),
            "attempts_executed": len(run["attempts"]) if run else None,
            "trace_events": len(run["events"]) if run else None}


# -- ddmin ------------------------------------------------------------------------------------------
def _chunks(items: list, n: int) -> list[list]:
    n = max(1, min(n, len(items)))
    k, r = divmod(len(items), n)
    out, start = [], 0
    for i in range(n):
        end = start + k + (1 if i < r else 0)
        out.append(items[start:end])
        start = end
    return out


def minimize(store: Store | None, run_id: str, *, invariant: str | None = None, budget: dict | None = None,
             minimization_id: str | None = None, cancel: Callable[[], bool] | None = None,
             executor: Callable[..., dict] = run_and_save, original_run: dict | None = None) -> dict:
    b = {**DEFAULT_BUDGET, **(budget or {})}
    orig = original_run or (store.get_run(run_id) if store is not None else None)
    if orig is None:
        raise KeyError(f"no run {run_id}")
    if orig.get("status", "completed") != "completed" or "invariants" not in orig:
        raise ValueError(f"run {run_id} is not completed")
    failed = [i["id"] for i in orig["invariants"] if i["status"] == "fail"]
    if not failed or orig.get("infra_error"):
        raise ValueError(f"run {run_id} has no confirmed invariant violation (verdict {orig.get('verdict')})")
    target_inv = invariant or failed[0]
    target = signature(orig, target_inv)
    if target is None:
        raise ValueError(f"invariant {target_inv} does not FAIL in run {run_id} (failed: {failed})")
    sched0 = orig["schedule"]
    orig_sha = store.run_record_sha256(run_id) if store is not None else None
    # External candidates: candidates run under the isolation configuration recorded on the original run.
    recorded = (orig.get("isolation") or {}).get("config")
    iso = {"isolation": recorded} if recorded is not None else {}
    config = {"run_id": run_id, "invariant": target_inv, "scenario_id": orig["scenario_id"], "impl_id": orig["impl_id"],
              "seed": orig["seed"], "budget": b, "original_schedule_id": schedule_id(sched0),
              "original_run_record_sha256": orig_sha, "target_signature": target,
              "strategy": "ddmin over removable schedule units (unit deletion only)"}
    mid = minimization_id
    if store is not None and mid is None:
        mid = store.create_analysis("minimization", config, status="running")
    elif store is not None:
        store.update_analysis(mid, status="running", config=config)
    t0 = time.monotonic()
    units = extract_units(sched0)
    unit_by_id = {u["id"]: u for u in units}
    attempts: list[dict] = []
    tested: dict[frozenset, bool] = {}
    unjudged: set[frozenset] = set()  # rejected without a verdict (infrastructure error, timeout, budget)
    state = {"status": "running", "stop_reason": None, "budget_exhausted": False}
    accepted_run: dict | None = None
    current = [u["id"] for u in units]

    def execute(schedule: dict, kind: str) -> dict:
        return executor(store, orig["scenario_id"], orig["impl_id"], orig["seed"], schedule=schedule,
                        origin={"kind": "minimization", "id": mid, "step": kind},
                        time_limit_s=b["run_time_limit_s"], max_events=None, **iso)

    def snapshot_result(final: bool = False) -> dict:
        kept_units = [unit_by_id[u] for u in current]
        removed = [u for u in units if u["id"] not in set(current)]
        res = {**state, "units_total": len(units), "units_kept": len(current),
               "attempts": attempts, "attempts_used": executions_used(),
               "retained_units": [{"id": u["id"], "kind": u["kind"], "desc": u["desc"]} for u in kept_units],
               "removed_units": [{"id": u["id"], "kind": u["kind"], "desc": u["desc"]} for u in removed],
               "original": {"run_id": run_id, "size": size_of(sched0, orig), "signature": target},
               "elapsed_s": round(time.monotonic() - t0, 1),
               "minimality": "1-minimal with respect to unit deletion" if state.get("one_minimal") else
                             ("not proven 1-minimal (some single-unit removals were never judged; see note)"
                              if state.get("one_minimal_unproven_units") else
                              "not proven 1-minimal (budget, interruption or error ended the search)") if final else None}
        if accepted_run is not None:
            res["current_best"] = {"run_id": accepted_run["id"], "size": size_of(accepted_run["schedule"], accepted_run)}
        return res

    def persist(final=False):
        if store is not None:
            store.update_analysis(mid, status=state["status"], result=snapshot_result(final))

    def executions_used() -> int:
        return sum(len(a.get("run_ids") or []) for a in attempts)

    def over_budget() -> str | None:
        if cancel and cancel():
            return "interrupted by request"
        if executions_used() >= b["max_attempts"]:
            return f"max_attempts ({b['max_attempts']}) reached"
        if time.monotonic() - t0 > b["max_duration_s"]:
            return f"max_duration_s ({b['max_duration_s']}) reached"
        return None

    def test(kept: list[str], phase: str) -> bool:
        key = frozenset(kept)
        if key in tested:
            return tested[key]
        cand = build_candidate(sched0, orig.get("decisions", {}), units, kept,
                               derived_from={"kind": "minimization", "minimization_id": mid, "kept_units": len(kept)})
        rec = {"n": len(attempts) + 1, "phase": phase, "kept_units": len(kept),
               "removed": sorted(set(current) - set(kept)), "executed": False}
        problems = validate_schedule(cand)
        if problems:
            rec.update(result="invalid", reason="; ".join(problems[:3]))
            attempts.append(rec)
            tested[key] = False
            return False
        ok, reason, runs = True, "", []
        for _ in range(b["confirm_repeats"]):
            if executions_used() + len(runs) >= b["max_attempts"] and runs:
                ok, reason = False, "budget reached before the candidate was confirmed"
                break
            r = execute(cand, phase)
            runs.append(r)
            rec["executed"] = True
            oc = outcome_class(r)
            if r.get("infra_error"):
                ok, reason = False, f"{oc}: {(r['infra_error'] or '')[:160]}"
                break
            good, why = matches(target, signature(r, target_inv), b["orders_policy"])
            if not good:
                ok, reason = False, why
                break
        rec.update(result="accepted" if ok else "rejected", reason=reason or "same failure signature",
                   run_ids=[r["id"] for r in runs], outcome=outcome_class(runs[-1]),
                   failed_invariants=[i["id"] for i in runs[-1].get("invariants", []) if i["status"] == "fail"],
                   size=size_of(cand, runs[-1]) if not runs[-1].get("infra_error") else size_of(cand))
        attempts.append(rec)
        tested[key] = ok
        if not ok and runs and (runs[-1].get("infra_error") or reason.startswith("budget reached")):
            unjudged.add(key)
        if ok:
            nonlocal accepted_run
            accepted_run = runs[-1]
        persist()
        return ok

    try:
        if len(sched0["directives"]) > b["max_schedule_directives"]:
            state.update(status="rejected", stop_reason=f"schedule has {len(sched0['directives'])} directives "
                                                          f"(> max_schedule_directives {b['max_schedule_directives']})")
            persist(final=True)
            return {"id": mid, "config": config, "result": snapshot_result(final=True)}
        # 1. Reproduce the original failure before reducing anything.
        repro = []
        repro_run = None
        for _ in range(b["reproduce_repeats"]):
            r = execute(sched0, "reproduce-original")
            repro_run = r
            repro.append({"run_id": r["id"], "outcome": outcome_class(r),
                          "match": matches(target, signature(r, target_inv), b["orders_policy"])[0]})
        state["reproduction"] = repro
        hits = sum(1 for x in repro if x["match"])
        if hits < len(repro):
            state.update(status="flaky" if hits else "not_reproducible",
                         stop_reason=f"original failure reproduced in {hits}/{len(repro)} executions; "
                                     "minimization not attempted")
            persist(final=True)
            return {"id": mid, "config": config, "result": snapshot_result(final=True)}
        # 2. ddmin by complement removal.
        n = 2
        while current:
            stop = over_budget()
            if stop:
                state.update(stop_reason=stop, budget_exhausted=not stop.startswith("interrupted"),
                             status="interrupted" if stop.startswith("interrupted") else "budget_exhausted")
                break
            reduced = False
            for chunk in _chunks(current, n):
                if over_budget():
                    break
                kept = [u for u in current if u not in chunk]
                if test(kept, f"ddmin n={n}"):
                    current[:] = kept
                    n = max(n - 1, 2)
                    reduced = True
                    break
            if reduced:
                continue
            if over_budget():
                continue
            if n >= len(current):
                # Every single-unit removal was tried; it is a proof only if each one was judged.
                open_units = [u for u in current if frozenset(x for x in current if x != u) in unjudged]
                state["one_minimal"] = not open_units
                if open_units:
                    state["one_minimal_unproven_units"] = open_units
                    state["note"] = (f"not proven 1-minimal: removing {len(open_units)} unit(s) ended in an "
                                     "infrastructure error, timeout or budget stop, so it was never judged")
                break
            n = min(len(current), n * 2)
        if not current and state["status"] == "running":
            state["one_minimal"] = True  # every unit was removable: nothing is left to delete
        if state["status"] == "running":
            state["status"] = "completed"
        # 3. Canonical minimized schedule (exact choices) and an independent replay of it.
        final_kept_sched = (accepted_run["schedule"] if accepted_run is not None else sched0)
        final_run = accepted_run or None
        if final_run is None:
            state["note"] = "; ".join(filter(None, ["no reduction was accepted; the minimized schedule equals the original",
                                                    state.get("note")]))
            final_run = repro_run  # the reproduction of the original is the accepted execution
            canonical = sched0
        else:
            canonical = strictify(final_kept_sched, final_run)
        state["minimized"] = {
            "schedule": canonical, "schedule_id": schedule_id(canonical), "run_id": final_run["id"],
            "size": size_of(canonical, final_run), "signature": signature(final_run, target_inv), "replay": None}
        if state["status"] == "interrupted":
            # No execution after a cancellation: report the best accepted candidate without a replay.
            state["minimized"]["replay_note"] = "not replayed: the minimization was interrupted"
        else:
            replay_run = execute(canonical, "final-replay")
            sig = signature(replay_run, target_inv)
            state["minimized"]["signature"] = sig
            state["minimized"]["replay"] = {
                "run_id": replay_run["id"], "outcome": outcome_class(replay_run),
                "signature_match": matches(target, sig, b["orders_policy"])[0],
                "same_trace_as_accepted": replay_run.get("trace_hash") == final_run.get("trace_hash"),
                "same_outcome_as_accepted": replay_run.get("outcome_hash") == final_run.get("outcome_hash")}
        state["reduction"] = {
            "directives": [len(sched0["directives"]), len(canonical["directives"])],
            "operations_defined": [len(sched0["ops"]), len(canonical["ops"])],
            "attempts_executed": [len(orig["attempts"]), len(final_run["attempts"])],
            "trace_events": [len(orig["events"]), len(final_run["events"])],
            "units": [len(units), len(current)],
        }
    except Exception as exc:
        state.update(status="error", stop_reason=f"{type(exc).__name__}: {exc}")
    if store is not None:
        state["original_unchanged"] = store.run_record_sha256(run_id) == orig_sha
    persist(final=True)
    return {"id": mid, "config": config, "result": snapshot_result(final=True)}
