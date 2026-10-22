"""Registry of implementations under evaluation: references and mutants."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field

from gauntlet.impls import mutants
from gauntlet.impls.reference_a import ReferenceA
from gauntlet.impls.reference_b import ReferenceB


@dataclass(frozen=True)
class ImplementationInfo:
    id: str
    kind: str  # "reference" | "mutant"
    strategy: str  # "A" | "B"
    title: str
    description: str
    cls: type = field(repr=False, compare=False)
    base: str | None = None
    mutation: str | None = None  # which method was changed and how
    intended_invariant: str | None = None
    intended_scenarios: tuple[str, ...] = ()

    def to_dict(self) -> dict:
        d = asdict(self)
        d.pop("cls")
        d["intended_scenarios"] = list(self.intended_scenarios)
        return d


IMPLEMENTATIONS: dict[str, ImplementationInfo] = {}


def _reg(info: ImplementationInfo) -> None:
    IMPLEMENTATIONS[info.id] = info


_reg(ImplementationInfo(
    id="ref_a_trigger", kind="reference", strategy="A", cls=ReferenceA,
    title="Reference A: trigger-based synchronisation",
    description="Database trigger keeps total and total_cents equal on every write; atomic cursor-based "
                "backfill computed in-statement; drain-gated finalize and contract.",
))
_reg(ImplementationInfo(
    id="ref_b_dualwrite", kind="reference", strategy="B", cls=ReferenceB,
    title="Reference B: atomic dual-write + post-drain reconciliation",
    description="V2 writes both columns in one statement/transaction; total stays authoritative until "
                "V1 has drained; finalize reconciles every row before switching authority.",
))

_MUTANTS = [
    ("m1_stale_backfill", mutants.StaleBackfillA, "ref_a_trigger",
     "Backfill ignores concurrent legacy writes",
     "migrator_backfill_batch: reads totals in one statement, writes cents computed from that snapshot in "
     "later statements.",
     "S1_ACKED_DURABLE", ("S02_backfill_vs_legacy_writes",)),
    ("m2_nonatomic_dual_write", mutants.NonAtomicDualWriteB, "ref_b_dualwrite",
     "Dual-write split across two transactions",
     "v2_add_charge: total (+ idempotency record) in transaction 1, total_cents in transaction 2.",
     "S3_REPR_AGREEMENT", ("S03_lost_ack_and_retry",)),
    ("m3_retry_duplicates", mutants.RetryDuplicatesA, "ref_a_trigger",
     "Retry path duplicates a charge",
     "v2_add_charge: stores the idempotency key with an upsert but never checks it before applying.",
     "S4_NO_DUPLICATION", ("S03_lost_ack_and_retry",)),
    ("m4_premature_contract", mutants.PrematureContractA, "ref_a_trigger",
     "Compatibility removed before legacy instances drain",
     "assert_legacy_drained: no-op, so finalize/contract run while V1 instances are registered.",
     "C1_COMPATIBILITY", ("S05_premature_finalization",)),
    ("m5_backfill_skips_row", mutants.BackfillSkipsRowA, "ref_a_trigger",
     "Backfill cursor off-by-one skips a row",
     "_next_cursor: advances the cursor to last_id + 1 while the batch query uses id > cursor.",
     "R4_TERMINAL_STATE", ("S01_baseline_migration", "S04_crash_and_resume")),
    ("m6_float_cents", mutants.FloatCentsA, "ref_a_trigger",
     "Cents conversion through binary floating point",
     "_cents (V2 input parsing): int(float(text) * 100).",
     "S2_EXACT_CENTS", ("S06_unbackfilled_records",)),
    ("m7_assumes_backfilled", mutants.AssumesBackfilledA, "ref_a_trigger",
     "New version assumes every row was backfilled",
     "_add_charge_sql (V2): COALESCE(total_cents, 0) + amount instead of falling back to the legacy column.",
     "S7_LINEARIZABLE", ("S06_unbackfilled_records",)),
    ("m8_skip_reconcile", mutants.SkipReconcileB, "ref_b_dualwrite",
     "Finalize skips post-drain reconciliation",
     "migrator_finalize: checks only for NULLs; no reconciliation or agreement check before switching "
     "authority to total_cents.",
     "S1_ACKED_DURABLE", ("S01_baseline_migration",)),
]

for mid, cls, base, title, mutation, inv, scen in _MUTANTS:
    _reg(ImplementationInfo(
        id=mid, kind="mutant", strategy=IMPLEMENTATIONS[base].strategy, cls=cls, base=base, title=title,
        description=(cls.__doc__ or "").strip().split("\n\n")[0].replace("\n", " "),
        mutation=mutation, intended_invariant=inv, intended_scenarios=scen,
    ))


def get_info(impl_id: str) -> ImplementationInfo:
    try:
        return IMPLEMENTATIONS[impl_id]
    except KeyError:
        pass
    from gauntlet.candidates import is_external
    if is_external(impl_id):
        return external_info(impl_id)
    raise KeyError(f"unknown implementation {impl_id!r}")


def external_info(impl_id: str) -> ImplementationInfo:
    """Metadata of a registered external candidate. Its code is never imported in this process."""
    from gauntlet.candidates import candidate_id_of
    from gauntlet.candidates.snapshots import CandidateError, load_meta
    try:
        meta = load_meta(candidate_id_of(impl_id))
    except (CandidateError, ValueError) as exc:
        raise KeyError(f"unknown implementation {impl_id!r}: {exc}") from None
    m = meta["manifest"]
    return ImplementationInfo(
        id=impl_id, kind="candidate", strategy=m["strategy"], cls=None,
        title=f"External candidate {m['name']}",
        description=(m.get("description") or "").strip()[:500] or "external candidate implementation")


def is_known(impl_id: str) -> bool:
    try:
        get_info(impl_id)
        return True
    except KeyError:
        return False


def load_implementation(impl_id: str, sandbox: dict | None = None):
    """Instantiate an implementation inside an actor process (external candidates via the verified loader)."""
    from gauntlet.candidates import is_external
    if is_external(impl_id):
        from gauntlet.candidates.loader import load
        return load(impl_id, sandbox)
    return get_info(impl_id).cls()
