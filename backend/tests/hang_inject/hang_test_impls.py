"""Test-only implementation for the watchdog tests. NOT a benchmark implementation.

Its migrator expand sleeps for five minutes without issuing any statement, so the actor process ignores
every request from the controller (a worker that cannot be stopped cooperatively). Registered only in
processes that import it (the test process, and actor subprocesses via sitecustomize.py next to this file).
"""
import time

from gauntlet.impls.base import Db
from gauntlet.impls.reference_a import ReferenceA

TEST_IMPL_ID = "test_hang_on_expand"


class HangOnExpand(ReferenceA):
    """Expand sleeps for five minutes before issuing any statement (a worker that ignores the controller)."""

    def migrator_expand(self, db: Db, a: dict) -> dict:
        time.sleep(300)
        return super().migrator_expand(db, a)


def register() -> None:
    from gauntlet.impls.registry import IMPLEMENTATIONS, ImplementationInfo
    IMPLEMENTATIONS.setdefault(TEST_IMPL_ID, ImplementationInfo(
        id=TEST_IMPL_ID, kind="mutant", strategy="A", title="test-only: expand never returns",
        description=(HangOnExpand.__doc__ or "").strip(), cls=HangOnExpand, base="ref_a_trigger",
        mutation="test-only watchdog fixture", intended_invariant="R4_TERMINAL_STATE",
        intended_scenarios=("S01_baseline_migration",)))  # a catalog scenario keeps the registry consistent


SLOW_IMPL_ID = "test_slow_statement_on_expand"


class SlowStatementOnExpand(ReferenceA):
    """Expand issues one statement that runs for a minute (a trusted implementation whose statement exceeds the
    step timeout). Used by test_step_timeout_pg.py."""

    def migrator_expand(self, db: Db, a: dict) -> dict:
        db.execute("SELECT pg_sleep(60)")
        return super().migrator_expand(db, a)


def slow_info():
    from gauntlet.impls.registry import ImplementationInfo
    return ImplementationInfo(
        id=SLOW_IMPL_ID, kind="mutant", strategy="A", title="test-only: a statement exceeds the step timeout",
        description=(SlowStatementOnExpand.__doc__ or "").strip(), cls=SlowStatementOnExpand, base="ref_a_trigger",
        mutation="test-only step-timeout fixture", intended_invariant="R4_TERMINAL_STATE",
        intended_scenarios=("S01_baseline_migration",))


def register_slow() -> None:
    """Actor subprocesses only (sitecustomize.py). The test process registers it per test with monkeypatch,
    so it never stays in the registry of the test session."""
    from gauntlet.impls.registry import IMPLEMENTATIONS
    IMPLEMENTATIONS.setdefault(SLOW_IMPL_ID, slow_info())
