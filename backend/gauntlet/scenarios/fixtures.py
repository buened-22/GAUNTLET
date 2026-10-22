"""Small deterministic fixture scenarios for schedule exploration and minimization tests.

Fixtures are NOT part of the benchmark catalog (``SCENARIOS``): they are never run by
``gauntlet bench`` and do not change any benchmark count. They use the same Scenario type, schedule
language, runner and oracle as the catalog. Resolve them with ``gauntlet.schedules.resolve_scenario``.
"""

from __future__ import annotations

from gauntlet.scenarios.catalog import Scenario, _finish
from gauntlet.scenarios.script import Script

FIXTURES: dict[str, Scenario] = {}


def build_f01(s: Script) -> dict[int, str]:
    """One backfill batch against one legacy write, interleaved by an explicit or seeded choice.

    The interleave directive is marked ``"explore": True``; it contains exactly two operations, so the
    whole statement-level interleaving space of the race is small. Everything else is sequential.
    """
    initial = {i: s.amount() for i in range(1, 5)}
    ids = list(initial)
    s.stage("P0")
    s.spawn("v1-a", "v1")
    s.spawn("migrator", "migrator")
    s.stage("P1")
    s.op("migrator", "expand")
    s.spawn("v2-a", "v2")
    s.stage("P3", "one backfill batch races one legacy write (explored)")
    bf = s.define("migrator", "backfill_batch", batch_size=4)
    w = s.define("v1-a", "set_total", order_id=3, total=s.amount())
    s.interleave({"migrator": [bf], "v1-a": [w]})
    s.directives[-1]["explore"] = True
    s.backfill_until_done("migrator", batch_size=4)
    s.snapshot("backfilled")
    _finish(s, "migrator", ["v1-a"], "v2-a", ids)
    return initial


def _reg(sc: Scenario) -> None:
    FIXTURES[sc.id] = sc


_reg(Scenario(
    id="F01_backfill_race_probe", title="Fixture: one backfill batch races one legacy write",
    purpose="Test fixture for schedule exploration (not a benchmark scenario). A single backfill batch of "
            "four rows and a single legacy set_total on order 3 are interleaved at statement level. A "
            "backfill that computes cents from a stale read (m1_stale_backfill) loses the legacy write only "
            "if that write commits between the batch's read and its update of order 3.",
    preconditions=("4 seeded orders", "one v1 instance", "one migrator", "one v2 instance"),
    faults=("F3",),
    expected_behavior="References pass under every interleaving; m1_stale_backfill fails S1_ACKED_DURABLE "
                      "under the interleavings that place the legacy write inside the read-to-update window.",
    build_fn=build_f01,
))
