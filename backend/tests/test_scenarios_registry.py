"""Schedule determinism and catalogue consistency (no database needed)."""

from gauntlet.engine.runner import canonical_hash, schedule_definition
from gauntlet.faults import BY_ID as FAULTS
from gauntlet.impls.registry import IMPLEMENTATIONS
from gauntlet.oracle.invariants import BY_ID as INVARIANTS
from gauntlet.scenarios.catalog import SCENARIOS


def test_schedule_is_pure_function_of_scenario_and_seed():
    for sid in SCENARIOS:
        a, b = schedule_definition(sid, 5), schedule_definition(sid, 5)
        assert canonical_hash(a) == canonical_hash(b)
        assert canonical_hash(a) != canonical_hash(schedule_definition(sid, 6))


def test_every_directive_kind_is_executable():
    from gauntlet.engine.controller import Controller
    kinds = {d["kind"] for sid in SCENARIOS for d in schedule_definition(sid, 1)["directives"]}
    for k in kinds:
        assert hasattr(Controller, f"_d_{k}"), k


def test_calls_reference_defined_operations_and_spawned_actors():
    for sid in SCENARIOS:
        sched = schedule_definition(sid, 1)
        spawned = set()
        for d in sched["directives"]:
            if d["kind"] == "spawn":
                spawned.add(d["actor"])
            if "actor" in d and d["kind"] != "spawn":
                assert d["actor"] in spawned, (sid, d)
            if d["kind"] in ("call", "retry_until_ok", "repeat"):
                assert d["logical_id"] in sched["ops"], (sid, d)
            if d["kind"] == "interleave":
                for q in d["queues"].values():
                    assert all(lid in sched["ops"] for lid in q)


def test_scenario_faults_exist():
    for sc in SCENARIOS.values():
        for f in sc.faults:
            assert f in FAULTS


def test_mutant_metadata_is_complete_and_consistent():
    mutants = [i for i in IMPLEMENTATIONS.values() if i.kind == "mutant"]
    assert len(mutants) >= 2
    for m in mutants:
        assert m.base in IMPLEMENTATIONS and IMPLEMENTATIONS[m.base].kind == "reference"
        assert m.intended_invariant in INVARIANTS
        assert m.intended_scenarios and all(s in SCENARIOS for s in m.intended_scenarios)
        assert m.mutation and m.description
        # single-point mutation: subclass of its base reference implementation
        assert issubclass(m.cls, IMPLEMENTATIONS[m.base].cls)


def test_reference_implementations_expose_full_contract():
    ops = ["v1_create_order", "v1_set_total", "v1_add_charge", "v1_get_order",
           "v2_create_order", "v2_set_total", "v2_add_charge", "v2_get_order",
           "migrator_expand", "migrator_backfill_batch", "migrator_finalize", "migrator_contract",
           "app_register", "app_deregister"]
    for i in IMPLEMENTATIONS.values():
        for op in ops:
            assert callable(getattr(i.cls, op, None)), (i.id, op)


def test_amount_generator_produces_float_hostile_values():
    from gauntlet.money import parse_cents
    from gauntlet.scenarios.script import Script, float_hostile
    s = Script(1, "x")
    vals = [parse_cents(s.amount(hostile_p=1.0)) for _ in range(50)]
    assert all(float_hostile(v) for v in vals)
