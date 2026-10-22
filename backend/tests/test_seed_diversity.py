"""Seed-driven schedule diversity (validation audit, Finding 2), determinism, and preservation of the
fault opportunities each scenario exists to provide. No database needed."""

import pytest

from gauntlet.engine.runner import canonical_hash, schedule_definition
from gauntlet.scenarios.catalog import SCENARIOS
from schedule_shape import schedule_shape

WEAK = ["S01_baseline_migration", "S03_lost_ack_and_retry", "S04_crash_and_resume", "S05_premature_finalization"]
SEEDS = range(1, 31)


def shape(sid, seed):
    return schedule_shape(schedule_definition(sid, seed))


@pytest.mark.parametrize("sid", list(SCENARIOS))
def test_same_seed_reproduces_identical_schedule(sid):
    for seed in (1, 2, 3, 17):
        assert canonical_hash(schedule_definition(sid, seed)) == canonical_hash(schedule_definition(sid, seed))


@pytest.mark.parametrize("sid", WEAK)
def test_different_seeds_give_structurally_different_schedules(sid):
    # The shape excludes amounts, keys and interleave seed values: only ordering, targets, actors,
    # fault positions and interleaving workloads count.
    first = [shape(sid, s) for s in (1, 2, 3)]
    assert len(set(first)) == 3, f"{sid}: seeds 1-3 share a schedule shape"
    many = {shape(sid, s) for s in range(1, 21)}
    assert len(many) >= 15, f"{sid}: only {len(many)} distinct shapes over 20 seeds"


@pytest.mark.parametrize("sid", WEAK)
def test_weak_scenarios_have_seeded_interleaving(sid):
    for seed in (1, 2, 3):
        kinds = [d["kind"] for d in schedule_definition(sid, seed)["directives"]]
        assert "interleave" in kinds


# --- fault opportunities must survive every seed ---------------------------------------------------------

def _walk(sid, seed):
    sched = schedule_definition(sid, seed)
    return sched["directives"], sched["ops"], set(int(k) for k in sched["initial_orders"])


def _index(ds, pred, start=0):
    return next((i for i in range(start, len(ds)) if pred(ds[i])), None)


def _retried_later(ds, i, lid):
    return any(d["kind"] == "call" and d.get("retry") and d["logical_id"] == lid for d in ds[i + 1:]) or \
        any(d["kind"] == "retry_until_ok" and d["logical_id"] == lid for d in ds[i + 1:])


def _episodes(ds, ops, op, fault_kind):
    """(index, logical_id) of client calls of `op` immediately followed by the given fault."""
    out = []
    for i, d in enumerate(ds[:-2]):
        if d["kind"] != "call" or d.get("retry") or ops[d["logical_id"]]["op"] != op:
            continue
        nxt = ds[i + 1]
        if fault_kind == "lost" and nxt["kind"] == "lose_response" and nxt["actor"] == d["actor"]:
            out.append((i, d["logical_id"]))
        if fault_kind == "crash_after_commit" and nxt["kind"] == "run_until" and nxt["cond"] == "after_commit" \
                and ds[i + 2]["kind"] == "crash" and ds[i + 2]["actor"] == d["actor"]:
            out.append((i, d["logical_id"]))
    return out


@pytest.mark.parametrize("seed", SEEDS)
def test_s01_opportunities(seed):
    ds, ops, initial = _walk("S01_baseline_migration", seed)
    assert not any(d["kind"] in ("crash", "lose_response", "run_until") for d in ds)  # still fault-free
    bf = _index(ds, lambda d: d["kind"] == "backfill_until_done")
    drain = _index(ds, lambda d: d["kind"] == "drain")
    # m8 / m5 opportunity: a legacy write to an original row after the backfill pass, before the drain.
    late = [d for d in ds[bf:drain] if d["kind"] == "call" and ops[d["logical_id"]]["role"] == "v1"
            and ops[d["logical_id"]]["op"] in ("set_total", "add_charge")
            and ops[d["logical_id"]]["args"]["order_id"] in initial]
    assert late
    il = [d for d in ds[:bf] if d["kind"] == "interleave"]
    assert il and set(il[0]["queues"]) == {"v1-a", "v2-a"}


@pytest.mark.parametrize("seed", SEEDS)
def test_s03_opportunities(seed):
    ds, ops, initial = _walk("S03_lost_ack_and_retry", seed)
    fin = _index(ds, lambda d: d["kind"] == "call" and ops[d["logical_id"]]["op"] == "finalize")
    bf = _index(ds, lambda d: d["kind"] == "backfill_until_done")
    lost = _episodes(ds, ops, "add_charge", "lost")
    crash = _episodes(ds, ops, "add_charge", "crash_after_commit")
    # m3: a v2 charge whose first attempt committed is retried before finalize.
    assert any(i < fin and ops[l]["role"] == "v2" and _retried_later(ds, i, l) for i, l in lost + crash)
    # v1 lost-response + retry before finalize.
    assert any(i < fin and ops[l]["role"] == "v1" and _retried_later(ds, i, l) for i, l in lost)
    # m7: a v2 charge on an original row before the backfill.
    assert any(i < bf and ops[l]["role"] == "v2" and ops[l]["args"]["order_id"] in initial for i, l in lost + crash)
    # m2: after finalize, a v2 charge killed right after its first commit, then retried.
    assert any(i > fin and ops[l]["role"] == "v2" and _retried_later(ds, i, l) for i, l in crash)
    # every crashed actor is restarted before it is used again
    for i, d in enumerate(ds):
        if d["kind"] == "crash":
            assert ds[i + 1]["kind"] == "restart" and ds[i + 1]["actor"] == d["actor"]
    # background traffic never touches an episode's order (it must not overwrite the evidence of a fault)
    episode_orders = {ops[l]["args"]["order_id"] for _, l in lost + crash + _episodes(ds, ops, "set_total", "lost")}
    background = {ops[lid]["args"]["order_id"] for d in ds if d["kind"] == "interleave"
                  for q in d["queues"].values() for lid in q}
    assert episode_orders and not (episode_orders & background)


@pytest.mark.parametrize("seed", SEEDS)
def test_s04_opportunities(seed):
    ds, ops, _ = _walk("S04_crash_and_resume", seed)
    crashes = [i for i, d in enumerate(ds) if d["kind"] == "crash"]
    assert len(crashes) == 4
    crashed = []
    for i in crashes:
        call = next(ds[j] for j in range(i - 1, -1, -1) if ds[j]["kind"] == "call")
        until = ds[i - 1]
        assert until["kind"] == "run_until" and ds[i + 1]["kind"] == "restart"
        crashed.append((ops[call["logical_id"]]["op"], until["cond"]))
        assert _retried_later(ds, i, call["logical_id"])
    assert crashed[0][0] == "expand" and crashed[0][1] in ("before_write", "before_commit")
    assert crashed[1] in (("backfill_batch", "before_write"), ("backfill_batch", "before_commit"))
    assert crashed[2] == ("backfill_batch", "after_commit")
    assert crashed[3][0] == "finalize" and crashed[3][1] in ("before_write", "before_commit")
    repeated = {ops[d["logical_id"]]["op"] for d in ds if d["kind"] == "repeat"}
    assert repeated == {"expand", "backfill_batch", "finalize", "contract"}
    # expand is repeated twice with the same logical operation: once before any backfill (no data yet)
    # and once after the backfill completed, so a repeat that destroys total_cents is observable (v0.3.0).
    ex = next(d["logical_id"] for d in ds if d["kind"] == "call" and ops[d["logical_id"]]["op"] == "expand")
    expand_repeats = [i for i, d in enumerate(ds) if d["kind"] == "repeat" and d["logical_id"] == ex]
    first_backfill = _index(ds, lambda d: d.get("logical_id") in ops and ops[d["logical_id"]]["op"] == "backfill_batch"
                            or d["kind"] == "backfill_until_done")
    backfilled = _index(ds, lambda d: d["kind"] == "snapshot" and d.get("label") == "backfilled")
    drain = _index(ds, lambda d: d["kind"] == "drain")
    assert len(expand_repeats) == 2
    assert expand_repeats[0] < first_backfill
    assert backfilled < expand_repeats[1] < drain
    # Legacy writes happen while the backfill is in progress (interleaved with the retried batch).
    assert any(d["kind"] == "interleave" and "migrator" in d["queues"] and d["queues"]["v1-a"] for d in ds)


@pytest.mark.parametrize("seed", SEEDS)
def test_s05_opportunities(seed):
    ds, ops, initial = _walk("S05_premature_finalization", seed)
    first_drain = _index(ds, lambda d: d["kind"] == "drain")
    fin = _index(ds, lambda d: d["kind"] == "call" and ops[d["logical_id"]]["op"] == "finalize")
    con = _index(ds, lambda d: d["kind"] == "call" and ops[d["logical_id"]]["op"] == "contract")
    assert fin < con < first_drain  # both premature attempts happen while v1 is registered
    # m4 / C1: v1 requests are issued after the contract attempt and before any drain.
    after = [lid for d in ds[con:first_drain] if d["kind"] == "interleave"
             for a, q in d["queues"].items() if a.startswith("v1") for lid in q]
    assert any(ops[lid]["op"] in ("set_total", "add_charge") for lid in after)
    creates = {ops[l]["args"]["order_id"] for l in ops if ops[l]["op"] == "create_order"}
    assert {40, 41} <= creates
