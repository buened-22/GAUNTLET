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
