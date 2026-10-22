"""The initial scenario suite (six scenarios).

Every scenario drives the full migration P0 -> P5 so the recovery invariant
R4 is always applicable; each one concentrates its adversarial pressure on
one region of the protocol. Faults reference docs/FAULT_CATALOG.md.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Callable

from gauntlet.scenarios.script import Script


@dataclass(frozen=True)
class Scenario:
    id: str
    title: str
    purpose: str
    preconditions: tuple[str, ...]
    faults: tuple[str, ...]
    expected_behavior: str
    build_fn: Callable[[Script], dict[int, str]] = field(repr=False, compare=False)
    target_stage: str = "contracted"

    def build(self, seed: int) -> dict:
        s = Script(seed, self.id)
        initial = self.build_fn(s)
        return {"initial_orders": {int(k): v for k, v in initial.items()},
                "directives": s.directives, "ops": s.ops}

    def to_dict(self) -> dict:
        d = asdict(self)
        d.pop("build_fn")
        d["preconditions"] = list(self.preconditions)
        d["faults"] = list(self.faults)
        return d


def _initial(s: Script, n: int) -> dict[int, str]:
    return {i: s.amount() for i in range(1, n + 1)}


def _finish(s: Script, migrator: str, v1_actors: list[str], v2: str, ids: list[int]) -> None:
    """Common tail: drain legacy instances, finalize, contract, read everything back."""
    s.stage("P4", "drain legacy instances and finalize")
    for a in v1_actors:
        s.drain(a)
    fin = s.call(migrator, "finalize")
    s.run(migrator)
    s.retry_until_ok(migrator, fin)
    s.snapshot("finalized")
    s.stage("P5", "contract and operate on the new schema")
    con = s.call(migrator, "contract")
    s.run(migrator)
    s.retry_until_ok(migrator, con)
    for oid in ids:
        s.op(v2, "get_order", order_id=oid)
    s.op(v2, "add_charge", order_id=ids[0], amount=s.amount(1, 5_000))


# ---------------------------------------------------------------------------
def build_s01(s: Script) -> dict[int, str]:
    initial = _initial(s, 12)
    ids = list(initial)
    batch = s.rng.choice([3, 4, 5])
    s.stage("P0", "legacy application only")
    s.spawn("v1-a", "v1")
    s.run_ops("v1-a", s.random_ops("v1-a", ids, 3))
    s.snapshot("P0")
    s.stage("P1", "expand schema while legacy serves")
    s.spawn("migrator", "migrator")
    s.op("migrator", "expand")
    s.op("v1-a", "create_order", order_id=13, total=s.amount())
    s.snapshot("expanded")
    s.stage("P2", "old and new versions coexist (seeded statement-level interleaving)")
    s.spawn("v2-a", "v2")
    s.op("v2-a", "create_order", order_id=14, total=s.amount())
    pool = ids + [13, 14]
    s.interleave({"v1-a": s.random_ops("v1-a", pool, 4), "v2-a": s.random_ops("v2-a", pool, 4)})
    s.stage("P3", "backfill in bounded batches")
    s.backfill_until_done("migrator", batch_size=batch)
    s.snapshot("backfilled")
    # Legacy writes to rows the backfill has already passed, before the drain.
    late = s.rng.sample(ids, 2)
    for oid in late:
        if s.rng.random() < 0.5:
            s.op("v1-a", "set_total", order_id=oid, total=s.amount())
        else:
            s.op("v1-a", "add_charge", order_id=oid, amount=s.amount(1, 5_000))
    for oid in late:
        s.op("v2-a", "get_order", order_id=oid)
    _finish(s, "migrator", ["v1-a"], "v2-a", pool)
    return initial


def build_s02(s: Script) -> dict[int, str]:
    initial = _initial(s, 16)
    ids = list(initial)
    s.stage("P0")
    s.spawn("v1-a", "v1")
    s.spawn("v1-b", "v1")
    s.spawn("migrator", "migrator")
    s.stage("P1")
    s.op("migrator", "expand")
    s.spawn("v2-a", "v2")
    s.stage("P3", "backfill races with legacy writes")
    batch = 4
    for r in range(2):
        lo = r * batch + 1
        rows = list(range(lo, lo + batch))
        s.call("migrator", "backfill_batch", batch_size=batch)
        # Pause the batch after its reads, before its first write (F3).
        s.run_until("migrator", "before_write", fault="F3")
        a, b = s.rng.sample(rows, 2)
        s.op("v1-a", "set_total", order_id=a, total=s.amount())
        s.op("v1-b", "add_charge", order_id=b, amount=s.amount(1, 5_000))
        s.run("migrator")
    s.snapshot("after_scripted_races")
    # Seeded random statement-level interleaving of backfill and legacy writes.
    q_m = [s.define("migrator", "backfill_batch", batch_size=batch) for _ in range(2)]
    q_a = s.random_ops("v1-a", ids, 5, writes_only=True)
    q_b = s.random_ops("v1-b", ids, 5)
    s.interleave({"migrator": q_m, "v1-a": q_a, "v1-b": q_b}, fault="F3")
    s.backfill_until_done("migrator", batch_size=batch)
    s.snapshot("backfilled")
    for oid in ids:
        s.op("v2-a", "get_order", order_id=oid)
    _finish(s, "migrator", ["v1-a", "v1-b"], "v2-a", ids)
    return initial


def _retry_with_background(s: Script, actor: str, lid: str, background: str, ids: list[int]) -> None:
    """Retry `lid` on `actor`, statement-interleaved (seeded) with unrelated requests on `background`."""
    s.retry(actor, lid, run=False)
    s.interleave({actor: [], background: s.random_ops(background, ids, s.rng.randint(1, 3))})


def build_s03(s: Script) -> dict[int, str]:
    initial = _initial(s, 12)
    ids = list(initial)
    v2s = ["v2-a", "v2-b"]
    s.stage("P0")
    s.spawn("v1-a", "v1")
    s.spawn("migrator", "migrator")
    s.stage("P1")
    s.op("migrator", "expand")
    s.stage("P2", "lost acknowledgements and retries (seeded order, targets, instances, interleaving)")
    s.spawn("v2-a", "v2")
    s.spawn("v2-b", "v2")
    targets = s.rng.sample(ids, 6)
    # Background requests never touch an episode's order, so they cannot overwrite the evidence of a fault.
    others = [i for i in ids if i not in targets]

    def other(x: str) -> str:
        return v2s[1 - v2s.index(x)]

    # F1 (response lost after commit) + F2 (retry with the same key).
    def v2_charge_lost(oid: int) -> None:
        x = s.rng.choice(v2s)
        lid = s.call(x, "add_charge", order_id=oid, amount=s.amount(1, 5_000))
        s.lose_response(x)
        _retry_with_background(s, s.rng.choice(v2s), lid, "v1-a", others)

    def v1_charge_lost(oid: int) -> None:
        lid = s.call("v1-a", "add_charge", order_id=oid, amount=s.amount(1, 5_000))
        s.lose_response("v1-a")
        _retry_with_background(s, "v1-a", lid, s.rng.choice(v2s), others)

    # F1 (process killed right after its first commit) + retry on the other instance.
    def v2_charge_crash(oid: int) -> None:
        x = s.rng.choice(v2s)
        lid = s.call(x, "add_charge", order_id=oid, amount=s.amount(1, 5_000))
        s.run_until(x, "after_commit", 1)
        s.crash(x, fault="F1")
        s.restart(x)
        _retry_with_background(s, other(x), lid, "v1-a", others)

    def v2_set_lost(oid: int) -> None:
        x = s.rng.choice(v2s)
        lid = s.call(x, "set_total", order_id=oid, total=s.amount())
        s.lose_response(x)
        _retry_with_background(s, other(x), lid, "v1-a", others)

    episodes = [v2_charge_lost, v1_charge_lost, v2_charge_crash, v2_set_lost]
    s.rng.shuffle(episodes)
    for episode, oid in zip(episodes, targets[:4]):
        episode(oid)
    for oid in targets[:4]:
        s.op(s.rng.choice(v2s), "get_order", order_id=oid)
    s.snapshot("after_retries")
    s.stage("P3")
    s.backfill_until_done("migrator", batch_size=s.rng.choice([2, 3, 4]))
    s.stage("P4", "drain, finalize, then more uncertain writes while both columns are authoritative")
    s.drain("v1-a")
    fin = s.call("migrator", "finalize")
    s.run("migrator")
    s.retry_until_ok("migrator", fin)

    def v2_charge_crash_finalized(oid: int) -> None:
        x = s.rng.choice(v2s)
        lid = s.call(x, "add_charge", order_id=oid, amount=s.amount(1, 5_000))
        s.run_until(x, "after_commit", 1)
        s.crash(x, fault="F1")
        s.restart(x)
        _retry_with_background(s, other(x), lid, x, others)

    def v2_charge_lost_finalized(oid: int) -> None:
        x = s.rng.choice(v2s)
        lid = s.call(x, "add_charge", order_id=oid, amount=s.amount(1, 5_000))
        s.lose_response(x)
        _retry_with_background(s, other(x), lid, x, others)

    late = [v2_charge_crash_finalized, v2_charge_lost_finalized]
    s.rng.shuffle(late)
    for episode, oid in zip(late, targets[4:6]):
        episode(oid)
    for oid in targets[4:6]:
        s.op(s.rng.choice(v2s), "get_order", order_id=oid)
    s.snapshot("finalized_with_retries")
    s.stage("P5")
    con = s.call("migrator", "contract")
    s.run("migrator")
    s.retry_until_ok("migrator", con)
    for oid in ids:
        s.op(s.rng.choice(v2s), "get_order", order_id=oid)
    return initial


def build_s04(s: Script) -> dict[int, str]:
    initial = _initial(s, 14)
    ids = list(initial)
    batch = s.rng.choice([3, 4])
    s.stage("P0")
    s.spawn("v1-a", "v1")
    s.spawn("migrator", "migrator")
    s.stage("P1", "expand interrupted by a worker crash (seeded crash point), then repeated")
    ex = s.call("migrator", "expand")
    s.run_until("migrator", s.rng.choice(["before_write", "before_commit"]))
    s.crash("migrator", fault="F7")
    s.restart("migrator")
    s.retry_until_ok("migrator", ex)
    s.repeat("migrator", ex)
    s.stage("P3", "backfill batches interrupted before and after commit (seeded positions)")
    for _ in range(s.rng.randint(0, 1)):
        s.op("migrator", "backfill_batch", batch_size=batch)
    b1 = s.call("migrator", "backfill_batch", batch_size=batch)
    s.run_until("migrator", s.rng.choice(["before_write", "before_commit"]))
    s.crash("migrator", fault="F4")
    s.restart("migrator")
    # The retried batch runs statement-interleaved (seeded) with legacy writes.
    s.retry("migrator", b1, fault=None, run=False)
    s.interleave({"migrator": [], "v1-a": s.random_ops("v1-a", ids, s.rng.randint(2, 3), writes_only=True)})
    s.retry_until_ok("migrator", b1)
    for _ in range(s.rng.randint(0, 1)):
        s.op("migrator", "backfill_batch", batch_size=batch)
    b2 = s.call("migrator", "backfill_batch", batch_size=batch)
    s.run_until("migrator", "after_commit")
    s.crash("migrator", fault="F4")
    s.restart("migrator")
    s.retry_until_ok("migrator", b2)
    s.backfill_until_done("migrator", batch_size=batch)
    last = s.call("migrator", "backfill_batch", batch_size=batch)
    s.run("migrator")
    s.repeat("migrator", last)
    s.snapshot("backfilled")
    # Repeat the completed expand again now that total_cents holds backfilled values, so a repeat
    # that destroys data is observable (the early repeat above runs before any data exists).
    s.repeat("migrator", ex)
    s.stage("P4", "finalize interrupted by a crash (seeded crash point), retried, repeated")
    s.drain("v1-a")
    fin = s.call("migrator", "finalize")
    s.run_until("migrator", s.rng.choice(["before_write", "before_commit"]))
    s.crash("migrator", fault="F7")
    s.restart("migrator")
    s.retry_until_ok("migrator", fin)
    s.repeat("migrator", fin)
    s.snapshot("finalized")
    s.stage("P5")
    con = s.call("migrator", "contract")
    s.run("migrator")
    s.retry_until_ok("migrator", con)
    s.repeat("migrator", con)
    s.spawn("v2-a", "v2")
    reads = list(ids)
    s.rng.shuffle(reads)
    for oid in reads:
        s.op("v2-a", "get_order", order_id=oid)
    return initial


def build_s05(s: Script) -> dict[int, str]:
    initial = _initial(s, 10)
    ids = list(initial)
    s.stage("P0")
    s.spawn("v1-a", "v1")
    s.spawn("v1-b", "v1")
    s.spawn("migrator", "migrator")
    s.stage("P1")
    s.op("migrator", "expand")
    s.spawn("v2-a", "v2")
    s.stage("P3")
    s.backfill_until_done("migrator", batch_size=s.rng.choice([3, 4, 5]))
    s.stage("P4", "finalization attempted while legacy instances still serve (F8; seeded interleaved traffic)")
    fin = s.call("migrator", "finalize")
    s.run("migrator")
    s.interleave({
        "v1-a": s.random_ops("v1-a", ids, s.rng.randint(1, 3), writes_only=True),
        "v1-b": [s.define("v1-b", "create_order", order_id=40, total=s.amount())]
        + s.random_ops("v1-b", ids, s.rng.randint(0, 2)),
        "v2-a": s.random_ops("v2-a", ids, s.rng.randint(1, 2)),
    })
    con = s.call("migrator", "contract")
    s.run("migrator")
    s.snapshot("after_premature_attempts")
    # Legacy instances keep serving after the premature contract attempt.
    s.interleave({
        "v1-a": s.random_ops("v1-a", ids, s.rng.randint(1, 2), writes_only=True),
        "v1-b": [s.define("v1-b", "create_order", order_id=41, total=s.amount())]
        + s.random_ops("v1-b", ids, s.rng.randint(0, 1)),
        "v2-a": s.random_ops("v2-a", ids, 1),
    })
    drains = ["v1-a", "v1-b"]
    s.rng.shuffle(drains)
    for a in drains:
        s.drain(a)
    s.retry_until_ok("migrator", fin)
    s.snapshot("finalized")
    s.stage("P5")
    s.retry_until_ok("migrator", con)
    for oid in ids + [40, 41]:
        s.op("v2-a", "get_order", order_id=oid)
    return initial


def build_s06(s: Script) -> dict[int, str]:
    initial = _initial(s, 10)
    ids = list(initial)
    s.stage("P0")
    s.spawn("v1-a", "v1")
    s.spawn("migrator", "migrator")
    s.stage("P1")
    s.op("migrator", "expand")
    s.stage("P2", "new version serves rows that are not yet backfilled (F6)")
    s.spawn("v2-a", "v2")
    s.op("v2-a", "get_order", order_id=1)
    s.op("v2-a", "add_charge", order_id=2, amount=s.amount(1, 5_000, hostile_p=1.0))
    s.op("v2-a", "add_charge", order_id=3, amount=s.amount(1, 5_000))
    s.op("v2-a", "set_total", order_id=4, total=s.amount(hostile_p=1.0))
    s.op("v2-a", "create_order", order_id=30, total=s.amount(hostile_p=1.0))
    s.op("v1-a", "add_charge", order_id=30, amount=s.amount(1, 5_000))
    s.op("v1-a", "get_order", order_id=2)
    s.op("v2-a", "get_order", order_id=4)
    q1 = s.random_ops("v1-a", ids, 5)
    q2 = s.random_ops("v2-a", ids, 6, hostile_p=0.7)
    s.interleave({"v1-a": q1, "v2-a": q2}, fault="F6")
    s.snapshot("coexistence")
    s.stage("P3")
    s.backfill_until_done("migrator", batch_size=4)
    s.snapshot("backfilled")
    _finish(s, "migrator", ["v1-a"], "v2-a", ids + [30])
    return initial


SCENARIOS: dict[str, Scenario] = {}


def _reg(sc: Scenario) -> None:
    SCENARIOS[sc.id] = sc


_reg(Scenario(
    id="S01_baseline_migration", title="Baseline expand-backfill-contract migration",
    purpose="Full P0-P5 migration with no injected faults. Legacy and new-version traffic is seeded: random "
            "targets and operations, a seeded statement-level interleaving of v1 and v2 during coexistence, and "
            "a seeded backfill batch size. Establishes that a correct implementation completes the protocol and "
            "that legacy writes made after the backfill pass are preserved.",
    preconditions=("12 seeded orders with random amounts (about 40% not exactly representable as binary floats)",
                   "one v1 instance, one v2 instance, one migrator"),
    faults=(),
    expected_behavior="All invariants hold; migration reaches the contracted state; every acknowledged write "
                      "is visible in the final total_cents.",
    build_fn=build_s01,
))
_reg(Scenario(
    id="S02_backfill_vs_legacy_writes", title="Backfill racing concurrent legacy writes",
    purpose="Pause each backfill batch after its reads and before its first write, let legacy instances "
            "write rows inside that batch, then resume. Followed by a seeded random statement-level "
            "interleaving of backfill batches and legacy writes.",
    preconditions=("16 seeded orders", "two v1 instances", "schema expanded"),
    faults=("F3",),
    expected_behavior="Backfill never overwrites a legacy write with a stale value; no acknowledged write is "
                      "lost; representations agree after the backfill.",
    build_fn=build_s02,
))
_reg(Scenario(
    id="S03_lost_ack_and_retry", title="Lost acknowledgements and idempotent retries",
    purpose="Writes commit but their responses are lost (response dropped, or the process is killed right "
            "after its first commit); clients retry with the same idempotency key, sometimes on another "
            "instance. Repeated after finalize, while both columns must stay in agreement. The seed chooses the "
            "episode order, target orders and instances, and the statement-level interleaving of each retry "
            "with unrelated background requests.",
    preconditions=("12 seeded orders (6 fault-episode targets; background traffic uses only the others)",
                   "one v1 and two v2 instances", "schema expanded"),
    faults=("F1", "F2"),
    expected_behavior="Each logical charge takes effect exactly once; retries return the original result; "
                      "representations agree after finalize.",
    build_fn=build_s03,
))
_reg(Scenario(
    id="S04_crash_and_resume", title="Migration worker crashes and resumes",
    purpose="The migrator process is hard-killed inside expand, inside a backfill batch (before and after "
            "commit) and inside finalize; it is restarted and the steps are retried. Completed steps are "
            "then executed again; expand is repeated both before any data exists and again after the backfill "
            "has populated total_cents. The seed chooses the crash points (before the first write or before COMMIT), "
            "which batches are interrupted, the batch size, and the interleaving of the retried batch with "
            "legacy writes.",
    preconditions=("14 seeded orders", "one v1 instance", "the migrator runs as a separate OS process"),
    faults=("F4", "F5", "F7", "F1"),
    expected_behavior="Interrupted steps leave no partial state and resume; repeated steps are no-ops; "
                      "the migration reaches the contracted state.",
    build_fn=build_s04,
))
_reg(Scenario(
    id="S05_premature_finalization", title="Finalization while legacy instances still serve",
    purpose="Finalize and contract are requested while v1 instances are registered and still writing. A "
            "correct implementation refuses until they drain. The legacy and new-version traffic around the "
            "premature attempts is a seeded statement-level interleaving; the drain order is seeded.",
    preconditions=("10 seeded orders", "two v1 instances and one v2 instance", "backfill complete"),
    faults=("F8",),
    expected_behavior="Premature finalize/contract are refused (an expected, explicit failure); no live v1 "
                      "request fails because of a schema change; the migration completes after the drain.",
    build_fn=build_s05,
))
_reg(Scenario(
    id="S06_unbackfilled_records", title="New version on records not yet backfilled",
    purpose="v2 reads, charges and overwrites rows before the backfill reaches them, using amounts that are "
            "not exactly representable as binary floats, mixed with a seeded random interleaving of v1 and v2.",
    preconditions=("10 seeded orders", "schema expanded, backfill not started"),
    faults=("F6",),
    expected_behavior="v2 derives correct values from the legacy column for unbackfilled rows; all amounts "
                      "remain exact to the cent.",
    build_fn=build_s06,
))


def get_scenario(scenario_id: str) -> Scenario:
    try:
        return SCENARIOS[scenario_id]
    except KeyError:
        raise KeyError(f"unknown scenario {scenario_id!r}") from None
