"""Test-only implementation for the R2 coverage regression test (v0.3.0). NOT a benchmark mutant.

It is registered only in processes that import it: the test process (directly) and actor subprocesses
started while this directory is on PYTHONPATH (via sitecustomize.py next to this file). It never appears
in the benchmark registry used by `gauntlet bench`.
"""
from gauntlet.impls.base import Db
from gauntlet.impls.reference_a import SYNC_FUNCTION_SQL, ReferenceA

TEST_IMPL_ID = "test_r2_expand_erases_on_repeat"


class ExpandErasesOnRepeat(ReferenceA):
    """On a repeated expand (stage already past 'legacy'), drop and recreate total_cents and the sync
    trigger, then report a no-op. migration_state is untouched; only the stored values are destroyed."""

    def migrator_expand(self, db: Db, a: dict) -> dict:
        with db.transaction():
            st = self._stage_exclusive(db)
            if st["stage"] != "legacy":
                db.execute("ALTER TABLE orders DROP COLUMN IF EXISTS total_cents")
                db.execute("ALTER TABLE orders ADD COLUMN total_cents BIGINT")
                db.execute(SYNC_FUNCTION_SQL)
                db.execute("DROP TRIGGER IF EXISTS orders_sync_total ON orders")
                db.execute("CREATE TRIGGER orders_sync_total BEFORE INSERT OR UPDATE ON orders "
                           "FOR EACH ROW EXECUTE FUNCTION gauntlet_sync_total()")
                return {"noop": True, "stage": st["stage"]}
        return super().migrator_expand(db, a)


def register() -> None:
    from gauntlet.impls.registry import IMPLEMENTATIONS, ImplementationInfo
    IMPLEMENTATIONS.setdefault(TEST_IMPL_ID, ImplementationInfo(
        id=TEST_IMPL_ID, kind="mutant", strategy="A", title="test-only: expand erases total_cents on repeat",
        description=(ExpandErasesOnRepeat.__doc__ or "").strip(), cls=ExpandErasesOnRepeat, base="ref_a_trigger",
        mutation="test-only regression fixture", intended_invariant="R2_REPEAT_SAFE",
        intended_scenarios=("S04_crash_and_resume",)))
