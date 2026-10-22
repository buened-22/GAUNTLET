"""External candidate implementations: contract, snapshots, isolation, loading and evaluation.

A *candidate* is a migration implementation produced outside GAUNTLET (for example by an AI coding
agent). It is validated statically, copied into an immutable content-addressed snapshot, and executed
only inside actor subprocesses under the isolation described in ``sandbox.py``. It is evaluated with the
existing runner, oracle, differential engine, explorer, minimizer and experiment reporting.

See docs/CANDIDATES.md for the contract, the isolation modes and their real security boundaries.
"""

CONTRACT_VERSION = "1"
LOADER_VERSION = "candidate-loader/1"
IMPL_PREFIX = "ext:"


def impl_id_for(candidate_id: str) -> str:
    return f"{IMPL_PREFIX}{candidate_id}"


def is_external(impl_id: str) -> bool:
    return isinstance(impl_id, str) and impl_id.startswith(IMPL_PREFIX)


def candidate_id_of(impl_id: str) -> str:
    if not is_external(impl_id):
        raise ValueError(f"{impl_id!r} is not an external candidate implementation id")
    return impl_id[len(IMPL_PREFIX):]
