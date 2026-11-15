"""Classification of a statement that exceeds the step timeout (audit finding FR-3), unit level: the evidence
rule decides "the statement itself was slow" only when every piece of evidence supports it."""

from __future__ import annotations

import pytest

from gauntlet.actor.proxy import step_timeout_attribution

STEP, STALL = 20.0, 2.0


def evidence(**over) -> dict:
    """Evidence of a healthy harness and server running pg_sleep for the whole step, cancelled with 57014."""
    ev = {"max_loop_gap_s": 0.05,
          "activity": {"state": "active", "wait_event_type": "Timeout", "wait_event": "PgSleep", "elapsed_s": 20.1},
          "cancel": "cancelled"}
    for k, v in over.items():
        if k in ("state", "wait_event_type", "wait_event", "elapsed_s"):
            ev["activity"] = {**ev["activity"], k: v}
        else:
            ev[k] = v
    return ev


def test_complete_evidence_is_attributed_to_the_statement():
    ok, why = step_timeout_attribution(evidence(), STEP, STALL)
    assert ok, why
    ok, why = step_timeout_attribution(evidence(wait_event_type=None, wait_event=None), STEP, STALL)  # on CPU
    assert ok, why
    ok, why = step_timeout_attribution(evidence(cancel="completed"), STEP, STALL)  # finished during the grace period
    assert ok, why


@pytest.mark.parametrize("over, expect", [
    ({"max_loop_gap_s": 3.5}, "stalled"),                                  # host or harness stall
    ({"max_loop_gap_s": None}, "stalled"),
    ({"activity": None}, "could not be read"),                             # server did not answer
    ({"state": "idle in transaction"}, "not executing"),
    ({"state": "idle"}, "not executing"),
    ({"elapsed_s": 4.0}, "whole step"),                                    # the server only just started it
    ({"elapsed_s": None}, "whole step"),
    ({"wait_event_type": "IO", "wait_event": "DataFileRead"}, "IO/DataFileRead"),  # could be a slow disk
    ({"wait_event_type": "LWLock", "wait_event": "WALWrite"}, "LWLock"),
    ({"wait_event_type": "Client", "wait_event": "ClientWrite"}, "Client"),   # waiting on the harness
    ({"wait_event_type": "Lock", "wait_event": "transactionid"}, "Lock"),
    ({"cancel": "not honoured within 5.0s"}, "did not end"),
    ({"cancel": "error (SQLSTATE None): server closed the connection"}, "did not end"),
    ({"cancel": "error (SQLSTATE 57P01): terminating connection"}, "did not end"),
    ({"cancel": None}, "did not end"),
])
def test_missing_or_unusual_evidence_is_never_attributed(over, expect):
    ok, why = step_timeout_attribution(evidence(**over), STEP, STALL)
    assert not ok and expect in why, why


@pytest.mark.parametrize("over", [
    {"max_loop_gap_s": 3.5}, {"activity": None}, {"wait_event_type": "IO", "wait_event": "DataFileRead"},
    {"elapsed_s": 1.0}, {"state": "idle"},
])
def test_sqlstate_57014_alone_is_not_sufficient(over):
    """Every case here has a clean 57014 cancel; each lacks one other piece of evidence and stays unattributed."""
    ev = evidence(**over)
    assert ev["cancel"] == "cancelled"
    ok, _ = step_timeout_attribution(ev, STEP, STALL)
    assert not ok
