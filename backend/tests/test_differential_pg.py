"""Differential testing end to end on PostgreSQL (real runs, temporary results store)."""

import ast
import pathlib

import pytest

from gauntlet.differential import replay_differential, run_differential
from gauntlet.store import Store

pytestmark = pytest.mark.pg
S02 = "S02_backfill_vs_legacy_writes"


@pytest.fixture(scope="module")
def store(tmp_path_factory):
    return Store(tmp_path_factory.mktemp("diff") / "results.sqlite3")


@pytest.fixture(scope="module")
def ref_vs_m1(store, pg_available):
    return run_differential(store, ["ref_a_trigger", "m1_stale_backfill"], S02, 1)


def test_known_fault_is_a_confirmed_violation_by_the_intended_invariant(ref_vs_m1):
    res = ref_vs_m1["result"]
    assert res["classification"] == "confirmed_violation"
    assert res["oracle"]["ref_a_trigger"]["verdict"] == "pass"
    assert "S1_ACKED_DURABLE" in res["oracle"]["m1_stale_backfill"]["failed_invariants"]  # m1's intended invariant
    fd = res["pairs"][0]["first_meaningful_divergence"]
    assert fd is not None and fd["a_context"] and fd["b_context"]


def test_equivalent_inputs_and_isolated_databases(ref_vs_m1):
    eq = ref_vs_m1["result"]["equivalence"]
    assert eq["same_schedule_hash"] and eq["same_initial_state_fingerprint"] and eq["isolated_databases"]
    dbs = list(eq["databases"].values())
    assert len(set(dbs)) == 2 and all(d.startswith("gauntlet_run_") for d in dbs)


def test_comparison_and_runs_are_persisted_and_retrievable(store, ref_vs_m1):
    a = store.get_analysis(ref_vs_m1["id"])
    assert a["kind"] == "differential" and a["status"] == "completed"
    assert a["result"]["classification"] == "confirmed_violation" and a["config"]["schedule"]["directives"]
    for r in a["result"]["runs"]:
        stored = store.get_run(r["id"])
        assert stored["origin"] == {"kind": "differential", "id": ref_vs_m1["id"]}
        assert stored["outcome_hash"] == r["outcome_hash"]


def test_two_correct_implementations_agree_despite_harmless_differences(store, pg_available):
    c = run_differential(store, ["ref_a_trigger", "ref_b_dualwrite"], S02, 1)
    res = c["result"]
    assert res["classification"] == "agreement", res["reason"]
    # Their raw migration payloads do differ (rule N3 is what makes this an agreement).
    ra, rb = (store.get_run(r["id"]) for r in res["runs"])
    raw = lambda run: {x["attempt_id"]: x["result"] for x in run["attempts"] if x["role"] == "migrator"}  # noqa: E731
    assert raw(ra) != raw(rb)


def test_oracle_judgement_does_not_depend_on_the_comparison_partner(ref_vs_m1, run_cache):
    alone = run_cache(S02, "m1_stale_backfill", 1)
    paired = next(r for r in ref_vs_m1["result"]["runs"] if r["impl_id"] == "m1_stale_backfill")
    assert paired["outcome_hash"] == alone["outcome_hash"] and paired["trace_hash"] == alone["trace_hash"]


def test_oracle_source_imports_no_implementation_code():
    base = pathlib.Path(__file__).resolve().parents[1] / "gauntlet" / "oracle"
    for f in base.glob("*.py"):
        for node in ast.walk(ast.parse(f.read_text(encoding="utf-8"))):
            if isinstance(node, (ast.Import, ast.ImportFrom)):
                names = [node.module or ""] if isinstance(node, ast.ImportFrom) else [n.name for n in node.names]
                assert not any(n.startswith("gauntlet.impls") or n.startswith("gauntlet.differential") for n in names), f


def test_replay_reproduces_the_comparison(store, ref_vs_m1):
    rep = replay_differential(store, ref_vs_m1["id"])
    assert rep["reproduced"] and rep["same_classification"], rep


def test_timeout_is_inconclusive_never_pass(store, pg_available):
    c = run_differential(store, ["ref_a_trigger", "ref_b_dualwrite"], S02, 1, time_limit_s=0.001)
    res = c["result"]
    assert res["classification"] == "inconclusive"
    assert {r["error_class"] for r in res["runs"]} == {"timeout"}
    assert all(r["verdict"] == "infra_error" for r in res["runs"])
