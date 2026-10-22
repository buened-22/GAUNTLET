"""External candidate contract: static validation, canonical hashing and immutable snapshots (no database)."""

import json
import os
import shutil
import stat
import subprocess
import sys
from pathlib import Path

import pytest

from gauntlet.candidates.contract import LIMITS, validate
from gauntlet.candidates.snapshots import CandidateError, list_candidates, load_meta, register, snapshot_dir, verify

FIXTURES = Path(__file__).resolve().parent / "candidates"
GOOD = FIXTURES / "synthetic_ref_a_copy"


@pytest.fixture(autouse=True)
def snapshots_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("GAUNTLET_CANDIDATES_DIR", str(tmp_path / "snapshots"))
    return tmp_path / "snapshots"


@pytest.fixture
def cand(tmp_path):
    """A writable copy of the valid synthetic candidate, outside the repository."""
    d = tmp_path / "cand"
    shutil.copytree(GOOD, d)
    return d


def manifest(d: Path) -> dict:
    return json.loads((d / "gauntlet_candidate.json").read_text(encoding="utf-8"))


def set_manifest(d: Path, m) -> None:
    (d / "gauntlet_candidate.json").write_text(m if isinstance(m, str) else json.dumps(m), encoding="utf-8")


def problems(d: Path, **kw) -> str:
    v = validate(d, **kw)
    assert not v.ok
    return " | ".join(v.problems)


def test_valid_candidate(cand):
    v = validate(cand)
    assert v.ok, v.problems
    assert [f["path"] for f in v.files] == ["candidate.py", "gauntlet_candidate.json"]
    assert len(v.source_hash) == 64


def test_missing_manifest_and_entry_point(cand):
    m = manifest(cand)
    m["entry"]["class"] = "Nope"
    set_manifest(cand, m)
    assert "entry class 'Nope' not defined" in problems(cand)
    m["entry"] = {"module": "missing_module", "class": "Implementation"}
    set_manifest(cand, m)
    assert "entry module 'missing_module' not found" in problems(cand)
    (cand / "gauntlet_candidate.json").unlink()
    assert "missing gauntlet_candidate.json" in problems(cand)


def test_unsupported_contract_version(cand):
    m = manifest(cand)
    m["contract_version"] = "2"
    set_manifest(cand, m)
    assert "unsupported contract_version '2'" in problems(cand)


@pytest.mark.parametrize("bad,expect", [
    ("{not json", "not valid JSON"),
    ("[1, 2]", "must be a JSON object"),
    ({"contract_version": "1", "name": "x", "entry": {"module": "candidate", "class": "Implementation"},
      "strategy": "A", "extra": 1}, "unknown keys ['extra']"),
    ({"contract_version": "1", "name": "../evil", "entry": {"module": "candidate", "class": "Implementation"},
      "strategy": "A"}, "name must match"),
    ({"contract_version": "1", "name": "x", "entry": {"module": "candidate", "class": "Implementation"},
      "strategy": "C"}, 'strategy must be "A" or "B"'),
    ({"contract_version": "1", "name": "x", "entry": {"module": "../candidate", "class": "Implementation"},
      "strategy": "A"}, "entry must be"),
])
def test_malformed_configuration(cand, bad, expect):
    set_manifest(cand, bad)
    assert expect in problems(cand)


def test_invalid_source_layout(cand):
    (cand / "run.sh").write_text("echo hi", encoding="utf-8")
    assert "run.sh: file type not allowed" in problems(cand)
    (cand / "run.sh").unlink()
    (cand / "binary.txt").write_bytes(b"\xff\xfe\x00bad")
    assert "binary.txt: not UTF-8 text" in problems(cand)
    (cand / "binary.txt").unlink()
    deep = cand / "a" / "b" / "c" / "d" / "e"
    deep.mkdir(parents=True)
    (deep / "x.py").write_text("x = 1\n", encoding="utf-8")
    assert "nesting deeper" in problems(cand)


def test_static_contract_checks(cand):
    src = (cand / "candidate.py").read_text(encoding="utf-8")
    (cand / "candidate.py").write_text(src + "\nimport subprocess\n", encoding="utf-8")
    assert "import of 'subprocess' is not allowed" in problems(cand)
    (cand / "candidate.py").write_text(src + "\nfrom gauntlet.oracle import checker\n", encoding="utf-8")
    assert "import of 'gauntlet.oracle' is not allowed" in problems(cand)
    (cand / "candidate.py").write_text(src + "\nfrom gauntlet.impls.reference_a import ReferenceA\n", encoding="utf-8")
    assert "gauntlet.impls.reference_a" in problems(cand)
    (cand / "candidate.py").write_text(src + "\nX = eval('1')\n", encoding="utf-8")
    assert "call to eval() is not allowed" in problems(cand)
    (cand / "candidate.py").write_text(src.replace("    def migrator_contract(", "    def migrator_contract_renamed("), encoding="utf-8")
    assert "missing required methods: ['migrator_contract']" in problems(cand)
    (cand / "candidate.py").write_text(src + "\n\nclass Implementation2(Implementation):\n    def v1_set_total(self, db, a):\n        return {}\n", encoding="utf-8")
    m = manifest(cand)
    m["entry"]["class"] = "Implementation2"
    set_manifest(cand, m)
    assert "harness-owned methods ['v1_set_total']" in problems(cand)
    (cand / "candidate.py").write_text("def broken(:\n", encoding="utf-8")
    assert "syntax error" in problems(cand)


def test_shadowing_harness_or_stdlib_modules_is_rejected(cand):
    (cand / "json.py").write_text("x = 1\n", encoding="utf-8")
    assert "would shadow" in problems(cand)
    (cand / "json.py").unlink()
    (cand / "gauntlet.py").write_text("x = 1\n", encoding="utf-8")
    assert "would shadow" in problems(cand)


def test_oversized_package(cand):
    (cand / "big.txt").write_text("x" * (LIMITS["max_file_bytes"] + 1), encoding="utf-8")
    assert "exceeds the per-file limit" in problems(cand)
    (cand / "big.txt").unlink()
    for i in range(6):  # 6 x 200 kB + the candidate > 1 MiB, each file under the per-file limit
        (cand / f"pad{i}.txt").write_text("y" * 200_000, encoding="utf-8")
    assert "total size limit" in problems(cand)


def _symlink(target: Path, link: Path, directory: bool) -> bool:
    try:
        link.symlink_to(target, target_is_directory=directory)
        return True
    except OSError:
        if directory and sys.platform.startswith("win"):  # junctions need no privilege on Windows
            r = subprocess.run(["cmd", "/c", "mklink", "/J", str(link), str(target)], capture_output=True)
            return r.returncode == 0
        return False


def test_symlink_and_junction_escapes_are_rejected(cand, tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.py").write_text("x = 1\n", encoding="utf-8")
    made_dir = _symlink(outside, cand / "linked_dir", directory=True)
    if made_dir:
        assert "linked_dir: directory link / junction is not allowed" in problems(cand)
        (cand / "linked_dir").rmdir() if (cand / "linked_dir").exists() else None
    made_file = _symlink(outside / "secret.py", cand / "link.py", directory=False)
    if made_file:
        assert "link.py: symbolic link / reparse point is not allowed" in problems(cand)
    if not (made_dir or made_file):
        pytest.skip("this environment can create neither symlinks nor junctions")


def test_hash_is_deterministic_and_content_sensitive(cand, tmp_path):
    h = validate(cand).source_hash
    # line endings, a UTF-8 BOM, file timestamps, excluded caches and the directory name do not matter
    crlf = tmp_path / "other-name"
    shutil.copytree(cand, crlf)
    p = crlf / "candidate.py"
    p.write_bytes(b"\xef\xbb\xbf" + p.read_bytes().replace(b"\n", b"\r\n"))
    os.utime(p, (1, 1))
    (crlf / "__pycache__").mkdir()
    (crlf / "__pycache__" / "candidate.cpython-314.pyc").write_bytes(b"\x00\x01")
    v = validate(crlf)
    assert v.ok and v.source_hash == h and v.excluded == ["__pycache__/"]
    # any content change produces a different hash
    p.write_bytes(p.read_bytes() + b"# changed\r\n")
    assert validate(crlf).source_hash != h


def test_hash_ignores_file_system_order(cand, tmp_path):
    (cand / "helpers.py").write_text("A = 1\n", encoding="utf-8")
    (cand / "zz_notes.md").write_text("notes\n", encoding="utf-8")
    other = tmp_path / "copy2"
    other.mkdir()
    for name in reversed(sorted(os.listdir(cand))):  # create files in the opposite order
        shutil.copy2(cand / name, other / name)
    assert validate(other).source_hash == validate(cand).source_hash


def test_register_is_immutable_idempotent_and_verified(cand, snapshots_dir):
    meta = register(cand)
    cid = meta["candidate_id"]
    assert cid == f"cand-{meta['source_hash'][:16]}" and not meta["already_registered"]
    assert str(cand) not in json.dumps(load_meta(cid)), "no host path is recorded"
    assert register(cand)["already_registered"]
    assert verify(cid)["source_hash"] == meta["source_hash"]
    assert [c["candidate_id"] for c in list_candidates()] == [cid]
    # editing the source after registration gives a NEW candidate; the first snapshot is untouched
    (cand / "candidate.py").write_text((cand / "candidate.py").read_text(encoding="utf-8") + "# v2\n", encoding="utf-8")
    meta2 = register(cand)
    assert meta2["candidate_id"] != cid and verify(cid)["source_hash"] == meta["source_hash"]
    # tampering with a stored snapshot is detected
    f = snapshot_dir(cid) / "src" / "candidate.py"
    os.chmod(f, stat.S_IWRITE)
    f.write_text(f.read_text(encoding="utf-8") + "# tampered\n", encoding="utf-8")
    with pytest.raises(CandidateError, match="modified after registration"):
        verify(cid)


def test_snapshot_files_are_read_only(cand):
    cid = register(cand)["candidate_id"]
    f = snapshot_dir(cid) / "src" / "candidate.py"
    assert not os.access(f, os.W_OK)


def test_rejected_candidates_are_not_registered(cand, snapshots_dir):
    m = manifest(cand)
    m["contract_version"] = "9"
    set_manifest(cand, m)
    with pytest.raises(CandidateError, match="unsupported contract_version"):
        register(cand)
    assert list_candidates() == []


@pytest.mark.parametrize("bad", ["../etc", "cand-zzzzzzzzzzzzzzzz", "cand-0123", "C:\\x", "cand-0123456789abcdef/.."])
def test_malformed_candidate_ids_never_become_paths(bad):
    with pytest.raises(CandidateError, match="malformed candidate id"):
        snapshot_dir(bad)
