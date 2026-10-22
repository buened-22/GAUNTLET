"""Schedule exploration: generation, bounds, completeness and termination (toy executor, no database).

The fake executor applies the controller's exact choice semantics (strict explicit prefix, then the
non-preemptive default, same preemption definition) to a two-actor system with fixed step counts:
actor "a" takes 5 steps (invoke, read, write, commit, respond) and "b" takes 3. The toy "bug": b runs
completely between a's read (step 2) and a's write (step 3).
"""

import itertools

import pytest

from gauntlet.explore import explore, failure_class
from gauntlet.schedules import build_schedule

STEPS = {"a": 5, "b": 3}


def toy_schedule():
    base = build_schedule("F01_backfill_race_probe", 1)
    for d in base["directives"]:
        if d.get("explore"):
            d["queues"] = {"a": ["L002"], "b": ["L003"]}
    return base


def simulate(choices, steps=STEPS, nondeterministic_at=None):
    done = {k: 0 for k in steps}
    pending, prev, log, order = list(choices), None, [], []
    while True:
        runnable = sorted(k for k in steps if done[k] < steps[k])
        if not runnable:
            if pending:
                return None, "explicit choices left over"
            return log, order
        if pending:
            c = pending.pop(0)
            if c not in runnable:
                return None, f"choice {c} not runnable"
            src = "choice"
        else:
            c, src = (prev if prev in runnable else runnable[0]), "default"
        if nondeterministic_at is not None and len(log) == nondeterministic_at:
            runnable = runnable  # same view, but flip the outcome below
        log.append({"step": len(log), "runnable": runnable, "chosen": c, "source": src,
                    "preemption": prev is not None and c != prev and prev in runnable})
        done[c] += 1
        order.append((c, done[c]))
        prev = c


def is_buggy(order):
    pos = {x: i for i, x in enumerate(order)}
    return all(pos[("a", 2)] < pos[("b", k)] < pos[("a", 3)] for k in range(1, 4))


def fake_executor_factory(calls, infra_on=None, steps=STEPS):
    def ex(store, scenario_id, impl_id, seed, *, schedule, origin, time_limit_s, max_events):
        idx = next(i for i, d in enumerate(schedule["directives"]) if d.get("explore"))
        log, order = simulate(schedule["directives"][idx]["choices"], steps)
        calls.append(schedule["directives"][idx]["choices"])
        rid = f"r{len(calls)}"
        if log is None:
            return {"id": rid, "verdict": "infra_error", "infra_error": f"schedule error: {order}", "error_class": "schedule_error",
                    "invariants": [], "decisions": {}, "duration_ms": 1.0}
        if infra_on is not None and len(calls) in infra_on:
            return {"id": rid, "verdict": "infra_error", "infra_error": "PostgreSQL unreachable", "error_class": "infrastructure",
                    "invariants": [], "decisions": {str(idx): log}, "duration_ms": 1.0}
        bug = is_buggy(order)
        inv = [{"id": "S1_ACKED_DURABLE", "status": "fail" if bug else "pass",
                "evidence": [{"order_id": 3, "explained_by": ["lost_ack"]}] if bug else []}]
        return {"id": rid, "verdict": "fail" if bug else "pass", "infra_error": None, "error_class": None,
                "invariants": inv, "decisions": {str(idx): log}, "duration_ms": 1.0,
                "trace_hash": str(order), "outcome_hash": str(bug)}
    return ex


def brute_force(k, steps=STEPS):
    """Every interleaving of the two actors with at most k preemptions, as choice sequences."""
    out = set()
    seq = ["a"] * steps["a"] + ["b"] * steps["b"]
    for perm in set(itertools.permutations(seq)):
        log, _ = simulate(list(perm), steps)
        if log is not None and sum(e["preemption"] for e in log) <= k:
            out.add(perm)
    return out


def run_explore(**kw):
    calls = []
    ex = kw.pop("executor", None) or fake_executor_factory(calls)
    r = explore(None, "F01_backfill_race_probe", "x", 1, base_schedule=toy_schedule(), executor=ex, **kw)
    return r["result"], calls


@pytest.mark.parametrize("k", [0, 1, 2, 3])
def test_explores_exactly_the_bounded_space(k):
    res, _ = run_explore(limits={"max_preemptions": k, "max_schedules": 1000}, replay_failures=False)
    explored = {tuple(s["choices"]) for s in res["schedules"]}
    assert explored == brute_force(k)
    assert len(explored) == len(res["schedules"]), "no schedule executed twice"
    assert res["status"] == "completed" and res["exhaustive_within_bounds"] and res["unexplored_known"] == 0
    assert all(s["preemptions"] <= k for s in res["schedules"])


def test_every_generated_schedule_respects_program_order():
    res, calls = run_explore(limits={"max_preemptions": 2, "max_schedules": 1000}, replay_failures=False)
    for s in res["schedules"]:
        assert s["choices"].count("a") == STEPS["a"] and s["choices"].count("b") == STEPS["b"]
    assert all(simulate(c)[0] is not None for c in calls), "every executed prefix was a valid schedule"


def test_generation_is_deterministic():
    r1, c1 = run_explore(limits={"max_preemptions": 2}, replay_failures=False)
    r2, c2 = run_explore(limits={"max_preemptions": 2}, replay_failures=False)
    assert c1 == c2 and [s["schedule_id"] for s in r1["schedules"]] == [s["schedule_id"] for s in r2["schedules"]]


def test_vulnerable_interleaving_needs_one_preemption_and_is_found_and_replayed():
    r0, _ = run_explore(limits={"max_preemptions": 0})
    assert r0["unique_failure_classes"] == 0  # the non-preemptive schedule alone is safe
    r1, _ = run_explore(limits={"max_preemptions": 1})
    assert r1["unique_failure_classes"] == 1
    c = r1["failure_classes"][0]
    assert c["failed_invariants"] == ["S1_ACKED_DURABLE"] and c["diagnosis_categories"] == ["lost_ack"]
    assert c["replay"]["reproduced"]
    bug = c["representative"]["choices"]
    assert is_buggy(simulate(bug)[1])


def test_max_schedules_limit_stops_and_reports_unexplored():
    res, calls = run_explore(limits={"max_preemptions": 3, "max_schedules": 5}, replay_failures=False)
    assert len(calls) == 5 and res["status"] == "stopped" and "max_schedules" in res["stop_reason"]
    assert res["unexplored_known"] > 0 and not res["exhaustive_within_bounds"] and res["unexplored_note"]


def test_depth_limit_is_disclosed_and_blocks_the_exhaustive_claim():
    res, _ = run_explore(limits={"max_preemptions": 2, "max_depth": 3}, replay_failures=False)
    assert res["counters"]["branch_points_beyond_depth"] > 0 and not res["exhaustive_within_bounds"]


def test_duration_limit_stops_exploration():
    res, _ = run_explore(limits={"max_preemptions": 3, "max_duration_s": -1}, replay_failures=False)
    assert res["status"] == "stopped" and "max_duration_s" in res["stop_reason"] and res["schedules_executed"] == 0


def test_cancel_interrupts_safely():
    n = {"i": 0}

    def cancel():
        n["i"] += 1
        return n["i"] > 3
    res, calls = run_explore(limits={"max_preemptions": 3}, cancel=cancel, replay_failures=False)
    assert res["status"] == "interrupted" and len(calls) == 3 and not res["exhaustive_within_bounds"]


def test_infrastructure_errors_are_retried_then_reported_and_block_exhaustiveness():
    calls = []
    ex = fake_executor_factory(calls, infra_on={1, 2})  # first execution and its retry both fail
    res, _ = run_explore(executor=ex, limits={"max_preemptions": 1, "max_retries": 1}, replay_failures=False)
    first = res["schedules"][0]
    assert first["outcome"] == "infrastructure" and first["retries"] == 1
    assert res["counters"]["errored"] == 1 and not res["exhaustive_within_bounds"]
    assert res["outcomes"].get("pass", 0) == 0 or "infrastructure" in res["outcomes"]


def test_infrastructure_error_once_then_success_after_retry():
    calls = []
    res, _ = run_explore(executor=fake_executor_factory(calls, infra_on={1}), limits={"max_preemptions": 0},
                         replay_failures=False)
    assert res["schedules"][0]["outcome"] == "pass" and res["schedules"][0]["retries"] == 1


def test_executor_exception_marks_error_not_complete():
    def boom(*a, **k):
        raise RuntimeError("store unavailable")
    res, _ = run_explore(executor=boom, limits={"max_preemptions": 1})
    assert res["status"] == "error" and "store unavailable" in res["stop_reason"] and not res["exhaustive_within_bounds"]


def test_failure_class_ignores_errors_and_passes():
    assert failure_class({"verdict": "pass", "infra_error": None, "invariants": []}) is None
    assert failure_class({"verdict": "infra_error", "infra_error": "x", "invariants": []}) is None
