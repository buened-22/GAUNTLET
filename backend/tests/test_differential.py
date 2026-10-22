"""Differential testing: normalization, comparison and classification (no database needed)."""

import copy

import pytest

from gauntlet.differential import NORMALIZATION_RULES, classify, compare_pair, validate_request


def attempt(aid, op, role, status="ok", result=None, error=None, seq=10):
    return {"attempt_id": aid, "logical_id": aid.split(".")[0], "op": op, "actor": f"{role}-a", "role": role,
            "status": status, "result": result, "error": error, "invoke_seq": seq, "complete_seq": seq + 1}


def run(rid, impl, attempts, rows, verdict="pass", fails=(), inconclusive=(), infra=None, columns=("id", "total_cents")):
    invs = [{"id": f, "status": "fail"} for f in fails] + [{"id": i, "status": "inconclusive"} for i in inconclusive]
    invs += [{"id": "S5_RECORDS_PRESERVED", "status": "pass"}]
    return {"id": rid, "impl_id": impl, "verdict": "infra_error" if infra else verdict, "infra_error": infra,
            "error_class": "infrastructure" if infra else None, "invariants": invs, "attempts": attempts,
            "events": [{"seq": i, "kind": "stmt", "t_ms": i * 1.5, "backend_pid": 100 + i} for i in range(1, 30)],
            "snapshots": [{"label": "final", "columns": list(columns), "rows": rows}]}


BASE_ATTEMPTS = [
    attempt("L001.a1", "expand", "migrator", result={"noop": False, "stage": "expanded"}, seq=1),
    attempt("L002.a1", "backfill_batch", "migrator", result={"updated": 2, "done": False, "cursor": 4}, seq=3),
    attempt("L003.a1", "set_total", "v1", result={"total": "9.78"}, seq=5),
    attempt("L004.a1", "get_order", "v2", result={"total": "9.78"}, seq=7),
]
BASE_ROWS = [{"id": 1, "total_cents": 978}, {"id": 2, "total_cents": 500}]


def pair(mutate_b, **kw_b):
    a = run("ra", "impl_a", copy.deepcopy(BASE_ATTEMPTS), copy.deepcopy(BASE_ROWS))
    b = run("rb", "impl_b", copy.deepcopy(BASE_ATTEMPTS), copy.deepcopy(BASE_ROWS), **kw_b)
    mutate_b(b)
    return a, b, compare_pair(a, b)


def test_identical_runs_agree():
    a, b, p = pair(lambda b: None)
    assert not p["differs"] and p["first_meaningful_divergence"] is None
    assert classify([a, b], [p])["classification"] == "agreement"


def test_timing_and_pids_are_ignored():  # N1
    def m(b):
        for e in b["events"]:
            e["t_ms"] += 999
            e["backend_pid"] += 7
    a, b, p = pair(m)
    assert not p["differs"] and p["first_statement_level_divergence_index"] is None


def test_migration_bookkeeping_payload_is_harmless():  # N3: the real ref A vs ref B difference
    def m(b):
        b["attempts"][1]["result"] = {"updated": 4, "done": False, "cursor": 4}
        b["attempts"][0]["result"] = {"noop": False, "stage": "expanded", "reconciled": 3}
    a, b, p = pair(m)
    assert not p["differs"]
    assert classify([a, b], [p])["classification"] == "agreement"


def test_error_message_text_is_ignored_but_code_is_not():  # N2
    a, b, p = pair(lambda b: b["attempts"][2].update(status="failed", result=None,
                                                       error={"code": "conflict", "message": "x"}))
    a2 = copy.deepcopy(b)
    a2["id"] = "ra2"
    a2["attempts"][2]["error"] = {"code": "conflict", "message": "different prose"}
    assert not compare_pair(a2, b)["differs"]
    a2["attempts"][2]["error"] = {"code": "not_found", "message": "x"}
    assert compare_pair(a2, b)["differs"]


@pytest.mark.parametrize("name,mutate", [
    ("missing record", lambda b: b["snapshots"][-1]["rows"].pop()),
    ("wrong amount", lambda b: b["snapshots"][-1]["rows"][0].update(total_cents=977)),
    ("acknowledgement differs", lambda b: b["attempts"][2].update(status="uncertain")),
    ("client result differs", lambda b: b["attempts"][3].update(result={"total": "2.72"})),
    ("migration op failed", lambda b: b["attempts"][1].update(status="failed", error={"code": "not_expanded"})),
    ("missing attempt", lambda b: b["attempts"].pop(2)),
    ("extra retry attempt", lambda b: b["attempts"].append(attempt("L003.a2", "set_total", "v1", result={"total": "9.78"}))),
    ("final schema differs", lambda b: b["snapshots"][-1].update(columns=["id", "total", "total_cents"])),
])
def test_meaningful_differences_survive_normalization(name, mutate):
    a, b, p = pair(mutate)
    assert p["differs"], name


def test_first_meaningful_divergence_points_at_earliest_differing_attempt():
    def m(b):
        b["attempts"][3]["result"] = {"total": "1.00"}
        b["attempts"][2]["result"] = {"total": "1.00"}
    a, b, p = pair(m)
    fd = p["first_meaningful_divergence"]
    assert fd["attempt_id"] == "L003.a1" and fd["a"]["result"] == {"total": "9.78"} and fd["b"]["result"] == {"total": "1.00"}
    assert fd["a_context"] and all("t_ms" not in e for e in fd["a_context"])


def test_disagreement_without_violation_is_unconfirmed():
    a, b, p = pair(lambda b: b["attempts"][3].update(result={"total": "2.72"}))
    c = classify([a, b], [p])
    assert c["classification"] == "unconfirmed_disagreement"


def test_disagreement_with_oracle_failure_is_confirmed():
    a, b, p = pair(lambda b: b["snapshots"][-1]["rows"].pop(), verdict="fail", fails=("S5_RECORDS_PRESERVED",))
    c = classify([a, b], [p])
    assert c["classification"] == "confirmed_violation"
    assert c["oracle"]["impl_b"]["failed_invariants"] == ["S5_RECORDS_PRESERVED"]


def test_infra_error_is_inconclusive_not_a_failure_or_agreement():
    a, b, p = pair(lambda b: None, infra="PostgreSQL unreachable")
    c = classify([a, b], [p])
    assert c["classification"] == "inconclusive" and "impl_b" in c["reason"]


def test_difference_with_only_inconclusive_oracle_is_inconclusive():
    a, b, p = pair(lambda b: b["attempts"][3].update(result={"total": "2.72"}), verdict="inconclusive",
                   inconclusive=("S7_LINEARIZABLE",))
    assert classify([a, b], [p])["classification"] == "inconclusive"


def test_agreement_on_a_shared_defect_is_flagged():
    a = run("ra", "impl_a", copy.deepcopy(BASE_ATTEMPTS), copy.deepcopy(BASE_ROWS), verdict="fail", fails=("S1_ACKED_DURABLE",))
    b = run("rb", "impl_b", copy.deepcopy(BASE_ATTEMPTS), copy.deepcopy(BASE_ROWS), verdict="fail", fails=("S1_ACKED_DURABLE",))
    c = classify([a, b], [compare_pair(a, b)])
    assert c["classification"] == "agreement"
    assert c["shared_violations"]["_common"] == ["S1_ACKED_DURABLE"] and "not that it is correct" in c["reason"]


def test_request_validation():
    assert validate_request(["ref_a_trigger"], "S02_backfill_vs_legacy_writes")
    assert validate_request(["ref_a_trigger", "ref_a_trigger"], "S02_backfill_vs_legacy_writes")
    assert validate_request(["ref_a_trigger", "nope"], "S02_backfill_vs_legacy_writes")
    assert validate_request(["ref_a_trigger", "ref_b_dualwrite"], "nope")
    assert validate_request(["ref_a_trigger", "ref_b_dualwrite"], "S02_backfill_vs_legacy_writes") == []
    assert validate_request(["ref_a_trigger", "m1_stale_backfill"], "F01_backfill_race_probe") == []


def test_every_normalization_rule_is_documented():
    assert [r["id"] for r in NORMALIZATION_RULES] == ["N1", "N2", "N3", "N4", "N5", "N6"]
    assert all(r["rule"] and r["why"] for r in NORMALIZATION_RULES)
