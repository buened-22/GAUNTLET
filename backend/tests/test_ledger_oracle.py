"""Ledger behaviour and oracle verdicts on synthetic (database-free) runs."""

import pytest

from gauntlet.oracle.checker import RunOracle, infra_results, verdict
from gauntlet.oracle.ledger import Completion, Invocation, Ledger, LedgerError


def inv(seq, aid, lid, op, args, role="v2", actor="v2-a", registered=True):
    return Invocation(seq=seq, attempt_id=aid, logical_id=lid, actor=actor, role=role, op=op, args=args,
                      registered=registered)


def test_ledger_is_append_only_and_monotonic():
    led = Ledger()
    led.append_invocation(inv(1, "L1.a1", "L1", "get_order", {"order_id": 1}))
    with pytest.raises(LedgerError):
        led.append_invocation(inv(2, "L1.a1", "L1", "get_order", {"order_id": 1}))
    with pytest.raises(LedgerError):
        led.append_completion(Completion(seq=1, attempt_id="L1.a1", status="ok", reason="delivered"))
    led.append_completion(Completion(seq=3, attempt_id="L1.a1", status="ok", reason="delivered", result={"total": "1.00"}))
    with pytest.raises(LedgerError):
        led.append_completion(Completion(seq=4, attempt_id="L1.a1", status="failed", reason="delivered"))
    with pytest.raises(LedgerError):
        led.append_completion(Completion(seq=5, attempt_id="nope", status="ok", reason="delivered"))
    assert not hasattr(led, "update") and not hasattr(led, "delete")
    assert [r["type"] for r in led.records()] == ["invocation", "completion"]


def test_ledger_copies_arguments():
    led = Ledger()
    args = {"order_id": 1}
    led.append_invocation(inv(1, "a", "L", "get_order", args))
    args["order_id"] = 2
    assert led.attempts()[0]["args"]["order_id"] == 1


def test_ledger_roundtrip():
    led = Ledger()
    led.append_invocation(inv(1, "a", "L", "get_order", {"order_id": 1}))
    led.append_completion(Completion(seq=2, attempt_id="a", status="uncertain", reason="crashed"))
    again = Ledger.from_records(led.records())
    assert again.attempts() == led.attempts()


def snapshot(seq, rows, columns=("id", "total"), label="s", not_null=None, triggers=()):
    return {"label": label, "seq": seq, "columns": list(columns), "rows": rows,
            "not_null": not_null or {}, "triggers": list(triggers)}


def attempt(seq_i, seq_c, aid, lid, op, args, status="ok", result=None, error=None, role="v1", actor="v1-a",
            reason="delivered", registered=True):
    return {"seq": seq_i, "attempt_id": aid, "logical_id": lid, "actor": actor, "role": role, "op": op,
            "args": args, "registered": registered, "invoke_seq": seq_i, "complete_seq": seq_c,
            "status": status, "reason": reason, "result": result, "error": error}


def statuses(results):
    return {r["id"]: r["status"] for r in results}


def test_oracle_detects_wrong_final_state_even_when_app_reported_success():
    # The implementation acknowledged set_total(5.00) and even returned it on a read,
    # but the database's authoritative column shows the old value.
    atts = [
        attempt(1, 2, "L1.a1", "L1", "set_total", {"order_id": 1, "total": "5.00", "key": "k1"},
                result={"total": "5.00"}),
        attempt(3, 4, "L2.a1", "L2", "get_order", {"order_id": 1}, result={"total": "5.00"}),
    ]
    snaps = [snapshot(10, [{"id": 1, "total": "10.00"}], label="final")]
    res = RunOracle(strategy="A", initial_orders={1: "10.00"}, attempts=atts, snapshots=snaps, repeats=[],
                    target_stage="none").evaluate()
    st = statuses(res)
    assert st["S7_LINEARIZABLE"] == "fail"
    assert st["S1_ACKED_DURABLE"] == "fail"
    assert verdict(res, None, []) == "fail"
    s7 = next(r for r in res if r["id"] == "S7_LINEARIZABLE")
    assert s7["evidence"][0]["order_id"] == 1
    assert s7["trigger_seq"] == 10


def test_oracle_passes_consistent_history():
    atts = [attempt(1, 2, "L1.a1", "L1", "add_charge", {"order_id": 1, "amount": "0.29", "key": "k"},
                    result={"total": "10.29"})]
    snaps = [snapshot(5, [{"id": 1, "total": "10.29"}], label="final")]
    res = RunOracle(strategy="A", initial_orders={1: "10.00"}, attempts=atts, snapshots=snaps, repeats=[],
                    target_stage="none").evaluate()
    st = statuses(res)
    assert st["S7_LINEARIZABLE"] == "pass" and st["S1_ACKED_DURABLE"] == "pass"
    assert st["R4_TERMINAL_STATE"] == "not_applicable"
    assert verdict(res, None, []) == "pass"


def test_non_canonical_amount_is_s2_violation():
    atts = [attempt(1, 2, "L1.a1", "L1", "get_order", {"order_id": 1}, result={"total": "10.0"})]
    res = RunOracle(strategy="A", initial_orders={1: "10.00"}, attempts=atts,
                    snapshots=[snapshot(5, [{"id": 1, "total": "10.00"}])], repeats=[], target_stage="none").evaluate()
    assert statuses(res)["S2_EXACT_CENTS"] == "fail"


def test_inconclusive_run_is_never_reported_as_pass():
    atts = [attempt(1, 2, "L1.a1", "L1", "get_order", {"order_id": 1}, result={"total": "10.00"})]
    res = RunOracle(strategy="A", initial_orders={1: "10.00"}, attempts=atts,
                    snapshots=[snapshot(5, [{"id": 1, "total": "10.00"}])], repeats=[], target_stage="none",
                    inconclusive=["settle: operations stuck on locks"]).evaluate()
    assert all(r["status"] != "fail" for r in res)
    assert verdict(res, None, ["settle: operations stuck on locks"]) == "inconclusive"


def test_infra_error_is_never_pass():
    res = infra_results("PostgreSQL unreachable")
    assert {r["status"] for r in res} == {"infra_error"}
    assert verdict(res, "PostgreSQL unreachable", []) == "infra_error"


def test_all_not_applicable_is_not_pass():
    res = [{"id": "X", "status": "not_applicable"}]
    assert verdict(res, None, []) == "inconclusive"


def test_representation_agreement_checked_per_strategy():
    cols = ("id", "total", "total_cents")
    expand = attempt(1, 2, "M1.a1", "M1", "expand", {}, role="migrator", actor="m", result={})
    snap = snapshot(5, [{"id": 1, "total": "10.00", "total_cents": 999}], columns=cols)
    a = RunOracle(strategy="A", initial_orders={1: "10.00"}, attempts=[expand], snapshots=[snap], repeats=[],
                  target_stage="none").evaluate()
    b = RunOracle(strategy="B", initial_orders={1: "10.00"}, attempts=[expand], snapshots=[snap], repeats=[],
                  target_stage="none").evaluate()
    assert statuses(a)["S3_REPR_AGREEMENT"] == "fail"  # A: trigger must keep them equal after expand
    assert statuses(b)["S3_REPR_AGREEMENT"] == "not_applicable"  # B: only required after finalize


def test_missing_record_and_compatibility_violation():
    atts = [attempt(1, 2, "L1.a1", "L1", "set_total", {"order_id": 2, "total": "1.00", "key": "k"}, status="failed",
                    error={"kind": "db", "code": "db_error", "sqlstate": "42703", "message": "column total"})]
    snaps = [snapshot(5, [{"id": 1, "total": "10.00"}])]
    res = RunOracle(strategy="A", initial_orders={1: "10.00", 2: "3.00"}, attempts=atts, snapshots=snaps,
                    repeats=[], target_stage="none").evaluate()
    st = statuses(res)
    assert st["S5_RECORDS_PRESERVED"] == "fail"
    assert st["C1_COMPATIBILITY"] == "fail"


def test_terminal_state_requires_introspected_contract():
    con = attempt(1, 2, "M1.a1", "M1", "contract", {}, role="migrator", actor="m", result={})
    good = snapshot(5, [{"id": 1, "total_cents": 1000}], columns=("id", "total_cents"),
                    not_null={"id": True, "total_cents": True})
    bad = snapshot(5, [{"id": 1, "total": "10.00", "total_cents": 1000}], columns=("id", "total", "total_cents"),
                   not_null={"id": True, "total_cents": False})
    ok = RunOracle(strategy="A", initial_orders={1: "10.00"}, attempts=[con], snapshots=[good], repeats=[]).evaluate()
    no = RunOracle(strategy="A", initial_orders={1: "10.00"}, attempts=[con], snapshots=[bad], repeats=[]).evaluate()
    assert statuses(ok)["R4_TERMINAL_STATE"] == "pass"
    assert statuses(no)["R4_TERMINAL_STATE"] == "fail"


# --- Regression: logical-operation identity is the idempotency key (validation audit, Finding 1) ----------

def _charge(seq_i, seq_c, aid, lid, key, out, status="ok", reason="delivered"):
    return attempt(seq_i, seq_c, aid, lid, "add_charge", {"order_id": 1, "amount": "1.00", "key": key},
                   status=status, result={"total": out} if status == "ok" else None, role="v2", actor="v2-a",
                   reason=reason)


def _run(atts, final_total):
    return RunOracle(strategy="A", initial_orders={1: "10.00"}, attempts=atts,
                     snapshots=[snapshot(20, [{"id": 1, "total": final_total}], label="final")], repeats=[],
                     target_stage="none").evaluate()


def test_key_reused_under_new_internal_id_correct_implementation_passes():
    # Two submissions with the same key but different internal logical ids; the server applies once
    # and replays the original response. Before the fix this was reported as a lost write (FAIL).
    res = _run([_charge(1, 2, "L1.a1", "L1", "k", "11.00"), _charge(3, 4, "L2.a1", "L2", "k", "11.00")], "11.00")
    st = statuses(res)
    assert st["S7_LINEARIZABLE"] == "pass" and st["S4_NO_DUPLICATION"] == "pass"
    assert verdict(res, None, []) == "pass"


def test_key_reused_under_new_internal_id_duplicating_implementation_fails():
    # Same submissions, but the implementation applies the charge twice. Before the fix this PASSED.
    res = _run([_charge(1, 2, "L1.a1", "L1", "k", "11.00"), _charge(3, 4, "L2.a1", "L2", "k", "12.00")], "12.00")
    st = statuses(res)
    assert st["S7_LINEARIZABLE"] == "fail"
    assert st["S4_NO_DUPLICATION"] == "fail"
    assert verdict(res, None, []) == "fail"


def test_lost_response_then_retry_under_new_internal_id_is_one_operation():
    first = _charge(1, 2, "L1.a1", "L1", "k", None, status="uncertain", reason="response_lost")
    once = _run([first, _charge(3, 4, "L2.a1", "L2", "k", "11.00")], "11.00")
    twice = _run([first, _charge(3, 4, "L2.a1", "L2", "k", "12.00")], "12.00")
    assert statuses(once)["S7_LINEARIZABLE"] == "pass"
    assert statuses(twice)["S4_NO_DUPLICATION"] == "fail"


def test_new_key_is_a_genuinely_new_operation():
    # Distinct keys with identical payloads are two charges: both must apply.
    both = _run([_charge(1, 2, "L1.a1", "L1", "k1", "11.00"), _charge(3, 4, "L2.a1", "L2", "k2", "12.00")], "12.00")
    one = _run([_charge(1, 2, "L1.a1", "L1", "k1", "11.00"), _charge(3, 4, "L2.a1", "L2", "k2", "11.00")], "11.00")
    assert statuses(both)["S7_LINEARIZABLE"] == "pass"
    assert statuses(one)["S7_LINEARIZABLE"] == "fail"


def test_key_reused_for_a_different_request_is_inconclusive_not_pass():
    a = _charge(1, 2, "L1.a1", "L1", "k", "11.00")
    b = attempt(3, 4, "L2.a1", "L2", "add_charge", {"order_id": 1, "amount": "5.00", "key": "k"},
                result={"total": "11.00"}, role="v2", actor="v2-a")
    res = _run([a, b], "11.00")
    s7 = next(r for r in res if r["id"] == "S7_LINEARIZABLE")
    assert s7["status"] == "inconclusive"
    assert "different request" in s7["summary"]
    assert verdict(res, None, []) != "pass"
