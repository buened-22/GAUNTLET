"""Load a candidate inside an actor process (never in the harness or API process).

Steps: verify the snapshot hash, install the guard, append the snapshot to the end of ``sys.path`` (so it
cannot shadow the standard library or harness modules), import the entry module, and compose the entry
class with the harness-owned legacy application (``LegacyV1``) and instance registration
(``ImplementationBase``).
"""

from __future__ import annotations

import importlib
import sys

from gauntlet.candidates import candidate_id_of
from gauntlet.candidates.snapshots import snapshot_dir, verify


def load(impl_id: str, sandbox: dict | None):
    from gauntlet.impls.base import ImplementationBase
    from gauntlet.impls.legacy import LegacyV1

    cid = candidate_id_of(impl_id)
    if sandbox and sandbox.get("snapshots_root"):  # the actor's environment is allow-listed: told by the harness
        import os
        os.environ["GAUNTLET_CANDIDATES_DIR"] = sandbox["snapshots_root"]
    meta = verify(cid)  # refuse a snapshot that changed after registration
    if not sandbox or not sandbox.get("workspace"):
        raise RuntimeError("candidate actors must run with a sandbox workspace")
    from gauntlet.candidates.guard import install
    install(sandbox["workspace"])
    src = str(snapshot_dir(cid) / "src")
    if src not in sys.path:
        sys.path.append(src)
    module = importlib.import_module(meta["manifest"]["entry"]["module"])
    cls = getattr(module, meta["manifest"]["entry"]["class"])
    for name in dir(cls):
        if name.startswith(("v1_", "app_")) and not hasattr(LegacyV1, name) and not hasattr(ImplementationBase, name):
            raise RuntimeError(f"candidate defines harness-owned method {name}")
    composed = type(f"Candidate_{cid}", (_HarnessOwned, cls, ImplementationBase), {"strategy": meta["manifest"]["strategy"]})
    return composed()


class _HarnessOwned:
    """Placed first in the MRO so the legacy application and registration always come from the harness."""

    from gauntlet.impls.legacy import LegacyV1 as _L
    from gauntlet.impls.base import ImplementationBase as _B
    v1_create_order = _L.v1_create_order
    v1_set_total = _L.v1_set_total
    v1_add_charge = _L.v1_add_charge
    v1_get_order = _L.v1_get_order
    app_register = _B.app_register
    app_deregister = _B.app_deregister
