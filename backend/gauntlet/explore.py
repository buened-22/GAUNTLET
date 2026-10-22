"""Bounded, reproducible schedule exploration (preemption-bounded systematic interleaving search).

What is explored
----------------
One ``interleave`` directive of a schedule (the one marked ``"explore": true``, else the first). At
each step of that directive the controller lets exactly one *runnable* actor take its next step: a
new invocation, one SQL statement grant, or the delivery of a response. A schedule is therefore a
sequence of actor choices. Ordering constraints are structural, not heuristic: each actor's own
operations and statements always stay in program order, an invocation precedes its statements,
which precede its response, and a statement blocked on a lock is not runnable until the lock is
released (detected with ``pg_blocking_pids``). Choices outside these constraints cannot be expressed.

Search
------
Stateless depth-first search with re-execution (each schedule runs on a fresh database):

* the root schedule uses the deterministic *non-preemptive* default (keep running the actor that
  took the previous step while it is runnable, otherwise the first runnable actor by name);
* every executed schedule's decision log yields children: for each step ``i`` past its forced
  prefix and each other runnable actor ``alt``, the prefix ``realized[:i] + [alt]`` followed by the
  default policy;
* a child is generated only if its number of *preemptions* (switching away from an actor that was
  still runnable) is at most ``max_preemptions``. The default policy never preempts, so a child's
  preemption count is known exactly before it runs.

If the frontier empties without errors, without nondeterministic replays and without branch points
skipped by ``max_depth``, every schedule of the explored directive with at most ``max_preemptions``
preemptions has been executed (assuming deterministic execution, which every replay checks). No
partial-order reduction is applied: independence of SQL statements is not assumed, so schedules
that differ only in the order of independent steps are all executed. Other directives (and any
other ``interleave``) are executed exactly as written.
"""

from __future__ import annotations

import copy
import time
from typing import Callable

from gauntlet.execution import outcome_class, run_and_save
from gauntlet.schedules import build_schedule, canonical_hash, derive_schedule, schedule_id
from gauntlet.store import Store

DEFAULT_LIMITS = {
    "max_schedules": 40,         # executed schedules (each one run on a fresh database)
    "max_preemptions": 1,        # preemption bound of generated schedules
    "max_depth": 400,            # decision steps at which alternatives are branched
    "max_events_per_run": 5000,  # trace events per execution (exceeding it -> limit_exceeded)
    "run_time_limit_s": 120,     # per execution (exceeding it -> timeout)
    "max_duration_s": 1800,      # whole exploration
    "max_retries": 1,            # re-executions of a schedule after an infrastructure error
}
RETRYABLE = ("infrastructure", "harness_exception")


def explored_directive(schedule: dict, index: int | None = None) -> int:
    ds = schedule["directives"]
    if index is not None:
        if not (0 <= index < len(ds)) or ds[index]["kind"] != "interleave":
            raise ValueError(f"directive {index} is not an interleave directive")
        return index
    marked = [i for i, d in enumerate(ds) if d["kind"] == "interleave" and d.get("explore")]
    any_il = [i for i, d in enumerate(ds) if d["kind"] == "interleave"]
    if not (marked or any_il):
        raise ValueError("schedule has no interleave directive to explore")
    return (marked or any_il)[0]


def with_choices(base: dict, index: int, choices: list[str], *, mode: str = "strict", derived_from: dict) -> dict:
    ds = copy.deepcopy(base["directives"])
    d = {k: v for k, v in ds[index].items() if k != "seed"}
    d.update(choices=list(choices), choice_mode=mode)
    ds[index] = d
    return derive_schedule(base, directives=ds, derived_from=derived_from)


def failure_class(run: dict) -> dict | None:
    """Symptom class of a failing run: failed invariants + oracle diagnosis categories.

    This groups failures by *what the oracle observed*, not by root cause: two schedules in one class
    show the same violated invariants with the same diagnosis categories; they may still have
    different causes, and failures in different classes may share one.
    """
    if run.get("infra_error") or run["verdict"] != "fail":
        return None
    failed = sorted(i["id"] for i in run["invariants"] if i["status"] == "fail")
    cats = sorted({c for i in run["invariants"] if i["status"] == "fail"
                   for e in (i.get("evidence") or []) if isinstance(e, dict) for c in (e.get("explained_by") or [])})
    key = {"failed_invariants": failed, "diagnosis_categories": cats}
    return {**key, "id": canonical_hash(key)[:12]}


def _count_preemptions(log: list[dict], upto: int) -> int:
    return sum(1 for e in log[:upto] if e.get("preemption"))


def explore(store: Store | None, scenario_id: str, impl_id: str, seed: int, *, base_schedule: dict | None = None,
            directive_index: int | None = None, limits: dict | None = None, exploration_id: str | None = None,
            cancel: Callable[[], bool] | None = None, replay_failures: bool = True,
            executor: Callable[..., dict] = run_and_save, isolation: dict | None = None) -> dict:
    lim = {**DEFAULT_LIMITS, **(limits or {})}
    base = base_schedule or build_schedule(scenario_id, seed)
    idx = explored_directive(base, directive_index)
    iso = {"isolation": isolation} if isolation is not None else {}  # external candidates only
    config = {"scenario_id": scenario_id, "impl_id": impl_id, "seed": seed, "directive_index": idx, **iso,
              "base_schedule": base, "base_schedule_id": schedule_id(base), "limits": lim,
              "algorithm": "preemption-bounded stateless DFS over one interleave directive; non-preemptive default; "
                           "no partial-order reduction"}
    eid = exploration_id
    if store is not None and eid is None:
        eid = store.create_analysis("exploration", config, status="running")
    elif store is not None:
        store.update_analysis(eid, status="running", config=config)
    t_start = time.monotonic()
    frontier: list[list[str]] = [[]]
    seen_realized: set[str] = set()
    schedules: list[dict] = []
    classes: dict[str, dict] = {}
    counters = {"pruned_by_preemption_bound": 0, "branch_points_beyond_depth": 0, "errored": 0,
                "nondeterministic_or_invalid": 0, "duplicates": 0, "retries": 0}
    stop_reason = None
    status = "running"

    def result(final: bool = False) -> dict:
        done = final and not frontier and status == "completed"
        exhaustive = (done and counters["errored"] == 0 and counters["nondeterministic_or_invalid"] == 0
                      and counters["branch_points_beyond_depth"] == 0 and counters["duplicates"] == 0)
        outcomes: dict[str, int] = {}
        for s in schedules:
            outcomes[s["outcome"]] = outcomes.get(s["outcome"], 0) + 1
        return {
            "status": status, "stop_reason": stop_reason,
            "schedules_executed": len(schedules), "outcomes": outcomes,
            "unexplored_known": len(frontier),
            "unexplored_note": ("pending schedules already generated but not executed; their own children are not yet "
                                "known, so this is a lower bound on the unexplored space within the bound")
            if frontier else None,
            "exhaustive_within_bounds": exhaustive,
            "exhaustive_claim": (f"every schedule of directive {idx} with at most {lim['max_preemptions']} preemption(s) "
                                 f"was executed") if exhaustive else None,
            "counters": counters, "schedules": schedules,
            "failure_classes": sorted(classes.values(), key=lambda c: -c["count"]),
            "unique_failure_classes": len(classes),
            "grouping": "failure class = (set of FAILed invariants, set of oracle diagnosis categories); a symptom "
                        "grouping, not a root-cause grouping",
            "elapsed_s": round(time.monotonic() - t_start, 1),
        }

    def persist(final: bool = False) -> None:
        if store is not None:
            store.update_analysis(eid, status=status, result=result(final))

    try:
        while frontier:
            if cancel and cancel():
                stop_reason, status = "interrupted by request", "interrupted"
                break
            if len(schedules) >= lim["max_schedules"]:
                stop_reason, status = f"max_schedules ({lim['max_schedules']}) reached", "stopped"
                break
            if time.monotonic() - t_start > lim["max_duration_s"]:
                stop_reason, status = f"max_duration_s ({lim['max_duration_s']}) reached", "stopped"
                break
            prefix = frontier.pop()
            sched = with_choices(base, idx, prefix, derived_from={"kind": "exploration", "exploration_id": eid,
                                                                   "directive_index": idx, "prefix": prefix})
            n = len(schedules) + 1
            tries = 0
            while True:
                run = executor(store, scenario_id, impl_id, seed, schedule=sched,
                                   origin={"kind": "exploration", "id": eid, "n": n},
                                   time_limit_s=lim["run_time_limit_s"], max_events=lim["max_events_per_run"], **iso)
                oc = outcome_class(run)
                if oc in RETRYABLE and tries < lim["max_retries"]:
                    tries += 1
                    counters["retries"] += 1
                    continue
                break
            log = run.get("decisions", {}).get(str(idx), [])
            realized = [e["chosen"] for e in log]
            entry = {"n": n, "run_id": run["id"], "prefix": prefix, "prefix_len": len(prefix), "choices": realized,
                     "steps": len(log), "preemptions": _count_preemptions(log, len(log)), "outcome": oc,
                     "verdict": run["verdict"], "retries": tries,
                     "failed_invariants": [i["id"] for i in run["invariants"] if i["status"] == "fail"],
                     "duration_ms": run["duration_ms"], "infra_error": run.get("infra_error")}
            canonical = with_choices(base, idx, realized, derived_from={"kind": "exploration", "directive_index": idx})
            entry["schedule_id"] = schedule_id(canonical)
            if oc == "schedule_error":
                counters["nondeterministic_or_invalid"] += 1
                entry["note"] = ("a prefix taken from an earlier execution was not executable: execution is not "
                                 "deterministic for this schedule (or the schedule is invalid)")
            elif run.get("infra_error"):
                counters["errored"] += 1
                entry["note"] = "no children generated: an errored execution's decision log cannot be trusted"
            else:
                key = canonical_hash(realized)
                duplicate = key in seen_realized
                if duplicate:  # possible only if execution is not deterministic; never expand it again
                    counters["duplicates"] += 1
                    entry["note"] = "duplicate of an already executed schedule (not expanded again)"
                seen_realized.add(key)
                fc = failure_class(run)
                if fc:
                    entry["failure_class"] = fc["id"]
                    c = classes.setdefault(fc["id"], {**fc, "count": 0, "schedule_numbers": [],
                                                      "representative": {"n": n, "run_id": run["id"],
                                                                         "schedule_id": entry["schedule_id"],
                                                                         "choices": realized,
                                                                         "trace_hash": run.get("trace_hash"),
                                                                         "outcome_hash": run.get("outcome_hash")}})
                    c["count"] += 1
                    c["schedule_numbers"].append(n)
                children = []
                for i in (range(len(prefix), len(log)) if not duplicate else ()):
                    alts = [a for a in log[i]["runnable"] if a != log[i]["chosen"]]
                    if not alts:
                        continue
                    if i >= lim["max_depth"]:
                        counters["branch_points_beyond_depth"] += 1
                        continue
                    before = _count_preemptions(log, i)
                    prev = log[i - 1]["chosen"] if i > 0 else None
                    for alt in alts:
                        pre = before + (1 if prev is not None and alt != prev and prev in log[i]["runnable"] else 0)
                        if pre > lim["max_preemptions"]:
                            counters["pruned_by_preemption_bound"] += 1
                            continue
                        children.append(realized[:i] + [alt])
                frontier.extend(reversed(children))  # explore earlier branch points first
            schedules.append(entry)
            persist()
        else:
            # The search ended because the frontier is empty. It is "completed" only if that frontier
            # covered the whole bounded space; otherwise it ended with gaps and is "incomplete".
            clean = (counters["errored"] == 0 and counters["nondeterministic_or_invalid"] == 0
                     and counters["branch_points_beyond_depth"] == 0 and counters["duplicates"] == 0)
            status = "completed" if clean else "incomplete"
            if not clean:
                stop_reason = ("frontier exhausted with gaps: errored executions, non-replayable prefixes, "
                               "duplicate schedules (nondeterminism) or branch points beyond max_depth (see counters)")
        # No further executions after a cancellation or once the duration budget is spent.
        skip_replays = status == "interrupted" or (stop_reason or "").startswith("max_duration_s")
        if replay_failures and skip_replays:
            for c in classes.values():
                c["replay"] = {"skipped": True, "reason": stop_reason, "reproduced": None}
        elif replay_failures:
            for c in classes.values():
                rep = c["representative"]
                canonical = with_choices(base, idx, rep["choices"],
                                         derived_from={"kind": "exploration-replay", "exploration_id": eid,
                                                       "directive_index": idx})
                again = executor(store, scenario_id, impl_id, seed, schedule=canonical,
                                     origin={"kind": "exploration-replay", "id": eid, "of_run": rep["run_id"]},
                                     time_limit_s=lim["run_time_limit_s"], max_events=lim["max_events_per_run"], **iso)
                same_trace = rep["trace_hash"] is not None and rep["trace_hash"] == again.get("trace_hash")
                same_outcome = rep["outcome_hash"] is not None and rep["outcome_hash"] == again.get("outcome_hash")
                c["replay"] = {"run_id": again["id"], "same_trace": same_trace, "same_outcome": same_outcome,
                               "same_choices": [e["chosen"] for e in again["decisions"].get(str(idx), [])] == rep["choices"],
                               "verdict": again["verdict"], "failure_class": (failure_class(again) or {}).get("id")}
                if again.get("infra_error"):  # the replay itself could not run: no evidence either way
                    c["replay"].update(reproduced=None, outcome=outcome_class(again),
                                       note="replay ended in an execution error; reproducibility not established")
                else:
                    c["replay"]["reproduced"] = (c["replay"]["same_trace"] and c["replay"]["same_outcome"]
                                                 and c["replay"]["same_choices"] and c["replay"]["failure_class"] == c["id"])
                    if not c["replay"]["reproduced"]:
                        c["flaky"] = True
    except Exception as exc:  # never report an aborted exploration as complete
        status, stop_reason = "error", f"{type(exc).__name__}: {exc}"
    persist(final=True)
    return {"id": eid, "config": config, "result": result(final=True)}


def canonical_schedule(exploration: dict, n: int) -> dict:
    """The exact, strictly replayable schedule of executed schedule number ``n`` of an exploration."""
    cfg, res = exploration["config"], exploration["result"]
    entry = next(s for s in res["schedules"] if s["n"] == n)
    return with_choices(cfg["base_schedule"], cfg["directive_index"], entry["choices"],
                        derived_from={"kind": "exploration", "directive_index": cfg["directive_index"]})
