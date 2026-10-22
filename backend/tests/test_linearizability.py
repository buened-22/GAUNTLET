"""Unit tests of the linearizability checker on hand-built histories."""

from gauntlet.oracle.linearizability import ABSENT, History, Item, check, first_conflict


def w(lid, kind, arg, *attempts, oid=1):
    """attempts: (status, start_seq, end_seq, out)"""
    return {"logical_id": lid, "order_id": oid, "kind": kind, "arg": arg,
            "attempts": [{"attempt_id": f"{lid}.a{i + 1}", "status": s, "window": (2 * a, 2 * b), "out": o}
                         for i, (s, a, b, o) in enumerate(attempts)]}


def snap(i, q, *values):
    return Item(id=f"s{i}", kind="read", outs=frozenset(values), intervals=((2 * q - 1, 2 * q + 1),), label=f"s{i}")


def read(i, a, b, value):
    return Item(id=f"r{i}", kind="read", outs=frozenset({value}), intervals=((2 * a, 2 * b),), label=f"r{i}")


def test_sequential_acked_writes_ok():
    h = History(1, 1000, [w("L1", "add", 50, ("ok", 1, 2, 1050)), w("L2", "set", 7, ("ok", 3, 4, None))],
                [snap(0, 5, 7)])
    assert check(h).ok is True


def test_lost_acked_write_detected_and_diagnosed():
    h = History(1, 1000, [w("L1", "set", 7, ("ok", 1, 2, None))], [snap(0, 5, 1000)])
    assert check(h).ok is False
    assert check(h, "lost_ack").ok is True
    assert check(h, "duplicate").ok is False
    assert check(h, "rounding").ok is False


def test_uncertain_write_may_or_may_not_apply():
    for observed in (1000, 1050):
        h = History(1, 1000, [w("L1", "add", 50, ("uncertain", 1, 2, None))], [snap(0, 5, observed)])
        assert check(h).ok is True
    h = History(1, 1000, [w("L1", "add", 50, ("uncertain", 1, 2, None))], [snap(0, 5, 1100)])
    assert check(h).ok is False


def test_retry_applies_at_most_once_and_duplicate_is_diagnosed():
    eff = w("L1", "add", 50, ("uncertain", 1, 2, None), ("ok", 3, 4, 1050))
    assert check(History(1, 1000, [eff], [snap(0, 5, 1050)])).ok is True
    bad = History(1, 1000, [w("L1", "add", 50, ("uncertain", 1, 2, None), ("ok", 3, 4, 1100))],
                  [snap(0, 5, 1100)])
    assert check(bad).ok is False
    assert check(bad, "duplicate").ok is True
    assert check(bad, "lost_ack").ok is False


def test_failed_write_must_not_apply():
    h = History(1, 1000, [w("L1", "set", 5, ("failed", 1, 2, None))], [snap(0, 5, 5)])
    assert check(h).ok is False
    assert check(h, "failed_applied").ok is True


def test_rounding_diagnosis():
    h = History(1, 1000, [w("L1", "set", 1999, ("ok", 1, 2, None))], [snap(0, 5, 1998)])
    assert check(h).ok is False
    assert check(h, "rounding").ok is True
    assert check(h, "lost_ack").ok is False


def test_real_time_order_is_respected():
    # set(7) completes before the read starts, so the read cannot return the old value.
    h = History(1, 1000, [w("L1", "set", 7, ("ok", 1, 2, None))], [read(0, 3, 4, 1000)])
    assert check(h).ok is False
    # Concurrent read may return either value.
    h2 = History(1, 1000, [w("L1", "set", 7, ("ok", 1, 4, None))], [read(0, 2, 3, 1000)])
    assert check(h2).ok is True


def test_create_semantics_and_absent_reads():
    h = History(5, ABSENT, [w("L1", "create", 300, ("ok", 3, 4, None), oid=5)], [snap(0, 2, ABSENT), snap(1, 6, 300)])
    assert check(h).ok is True
    h_bad = History(5, ABSENT, [w("L1", "create", 300, ("ok", 3, 4, None), oid=5)], [snap(0, 6, ABSENT)])
    assert check(h_bad).ok is False


def test_acked_add_output_is_checked():
    h = History(1, 1000, [w("L1", "add", 50, ("ok", 1, 2, 9999))], [])
    assert check(h).ok is False


def test_first_conflict_points_at_earliest_bad_observation():
    h = History(1, 1000, [w("L1", "set", 7, ("ok", 1, 2, None))], [snap(0, 3, 7), snap(1, 6, 1000), snap(2, 9, 1000)])
    c = first_conflict(h)
    assert c is not None and c.id == "s1"


def test_budget_exhaustion_is_inconclusive_not_pass():
    effs = [w(f"L{i}", "add", 1, ("uncertain", 1, 100, None)) for i in range(14)]
    h = History(1, 0, effs, [snap(0, 200, 999)])
    res = check(h, budget=50)
    assert res.ok is None
