"""Immutable, content-addressed candidate snapshots.

``register(path)`` validates a candidate directory and copies its normalized files into
``<GAUNTLET_HOME>/candidates/<candidate_id>/src``. The candidate id is ``cand-`` + the first 16 hex digits
of the canonical source hash, so the same source always maps to the same snapshot and any change to the
source produces a different id. A snapshot is written once (to a temporary directory, then renamed into
place), its files are marked read-only, and it is never overwritten; re-registering identical source
returns the existing snapshot. ``verify(candidate_id)`` re-hashes the snapshot before every use.

Only the base name of the source directory is recorded, never its full host path.
"""

from __future__ import annotations

import json
import os
import shutil
import stat
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path

from gauntlet.candidates import CONTRACT_VERSION, LOADER_VERSION, impl_id_for
from gauntlet.candidates.contract import collect, tree_hash, validate
from gauntlet.config import GAUNTLET_HOME

SNAPSHOT_META = "snapshot.json"


class CandidateError(ValueError):
    """A candidate was rejected (invalid, tampered with, or unknown)."""


def snapshots_root() -> Path:
    return Path(os.environ.get("GAUNTLET_CANDIDATES_DIR", GAUNTLET_HOME / "candidates"))


def snapshot_dir(candidate_id: str) -> Path:
    if not candidate_id.startswith("cand-") or not all(c in "0123456789abcdef" for c in candidate_id[5:]) \
            or len(candidate_id) != 21:
        raise CandidateError(f"malformed candidate id {candidate_id!r}")
    return snapshots_root() / candidate_id


def _make_read_only(root: Path) -> None:
    for dirpath, _dirs, files in os.walk(root):
        for f in files:
            p = Path(dirpath) / f
            os.chmod(p, stat.S_IREAD)


def register(source: Path, *, limits: dict | None = None) -> dict:
    v = validate(Path(source), limits)
    if not v.ok:
        raise CandidateError("candidate rejected: " + "; ".join(v.problems))
    cid = f"cand-{v.source_hash[:16]}"
    dest = snapshot_dir(cid)
    if dest.exists():
        meta = load_meta(cid)
        if meta["source_hash"] != v.source_hash:
            raise CandidateError(f"snapshot {cid} exists with a different source hash (prefix collision); refusing")
        verify(cid)
        return {**meta, "already_registered": True}
    meta = {
        "candidate_id": cid, "impl_id": impl_id_for(cid), "source_hash": v.source_hash,
        "contract_version": CONTRACT_VERSION, "loader_version": LOADER_VERSION,
        "manifest": v.manifest, "files": v.files, "excluded": v.excluded,
        "source_dir_name": Path(source).name, "registered_at": datetime.now(timezone.utc).isoformat(),
    }
    root = snapshots_root()
    root.mkdir(parents=True, exist_ok=True)
    tmp = Path(tempfile.mkdtemp(prefix=f".{cid}-", dir=root))
    try:
        for rel, data in v.contents.items():
            p = tmp / "src" / rel
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_bytes(data)
        (tmp / SNAPSHOT_META).write_text(json.dumps(meta, indent=1, sort_keys=True), encoding="utf-8")
        _rename_with_retry(tmp, dest)
        _make_read_only(dest)
    except OSError:
        shutil.rmtree(tmp, onerror=_force_remove)
        if dest.exists():  # a concurrent registration of identical source won the race
            return {**load_meta(cid), "already_registered": True}
        raise
    verify(cid)
    return {**meta, "already_registered": False}


def _rename_with_retry(src: Path, dest: Path, attempts: int = 20, delay: float = 0.25) -> None:
    """Atomic directory rename. On Windows a sync client or virus scanner can briefly hold handles on files
    that were just written (observed with OneDrive: WinError 5); retry for a bounded time, then fail."""
    for i in range(attempts):
        try:
            os.replace(src, dest)
            return
        except PermissionError:
            if i == attempts - 1 or dest.exists():
                raise
            time.sleep(delay)


def _force_remove(func, path, _exc):
    os.chmod(path, stat.S_IWRITE)
    func(path)


def load_meta(candidate_id: str) -> dict:
    p = snapshot_dir(candidate_id) / SNAPSHOT_META
    if not p.is_file():
        raise CandidateError(f"unknown candidate {candidate_id}")
    return json.loads(p.read_text(encoding="utf-8"))


def verify(candidate_id: str) -> dict:
    """Re-hash the snapshot; raise CandidateError if any file was added, removed or changed."""
    meta = load_meta(candidate_id)
    v = collect(snapshot_dir(candidate_id) / "src")
    if v.problems:
        raise CandidateError(f"snapshot {candidate_id} is not intact: " + "; ".join(v.problems))
    now = tree_hash(v.files)
    if now != meta["source_hash"] or [f["path"] for f in v.files] != [f["path"] for f in meta["files"]]:
        raise CandidateError(f"snapshot {candidate_id} was modified after registration "
                             f"(recorded {meta['source_hash'][:16]}, now {now[:16]})")
    return meta


def list_candidates() -> list[dict]:
    root = snapshots_root()
    out = []
    if root.is_dir():
        for d in sorted(root.iterdir()):
            if d.is_dir() and d.name.startswith("cand-") and (d / SNAPSHOT_META).is_file():
                m = json.loads((d / SNAPSHOT_META).read_text(encoding="utf-8"))
                out.append({k: m[k] for k in ("candidate_id", "impl_id", "source_hash", "contract_version",
                                              "registered_at", "source_dir_name")}
                           | {"name": m["manifest"]["name"], "strategy": m["manifest"]["strategy"],
                              "producer": m["manifest"].get("producer")})
    return out
