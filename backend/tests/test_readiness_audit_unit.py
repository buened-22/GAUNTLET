"""Re-audit regressions that need no database: provenance fingerprints and the enforcement record."""

import subprocess
import sys

import pytest

import gauntlet.schedules as sch
from gauntlet.candidates.sandbox import IsolationConfig, check, enforcement


def test_source_fingerprints_ignore_checkout_line_endings(tmp_path):
    lf, crlf = tmp_path / "lf.py", tmp_path / "crlf.py"
    lf.write_bytes(b"a = 1\nb = 2\n")
    crlf.write_bytes(b"a = 1\r\nb = 2\r\n")
    assert sch._file_sha(lf) == sch._file_sha(crlf)
    (tmp_path / "other.py").write_bytes(b"a = 1\nb = 3\n")
    assert sch._file_sha(tmp_path / "other.py") != sch._file_sha(lf)


def test_frozen_oracle_fingerprint_is_independent_of_crlf_conversion():
    import hashlib
    from pathlib import Path
    base = Path(sch.__file__).resolve().parent / "oracle"
    lf_doc = {p.name: hashlib.sha256(p.read_bytes().replace(b"\r\n", b"\n")).hexdigest() for p in sorted(base.glob("*.py"))}
    assert sch.oracle_fingerprint() == sch.canonical_hash(lf_doc)


def test_harness_source_change_since_process_start_is_reported(monkeypatch):
    assert sch.harness_source()["changed_since_process_start"] is False
    monkeypatch.setattr(sch, "_SOURCE_AT_PROCESS_START", "0" * 64)
    assert sch.harness_source()["changed_since_process_start"] is True


def test_git_state_is_not_cached_across_calls(monkeypatch):
    outputs = iter(["abc\n", "", "abc\n", " M backend/x.py\n"])

    def fake_run(*a, **k):
        return subprocess.CompletedProcess(a, 0, stdout=next(outputs), stderr="")
    monkeypatch.setattr(sch.subprocess, "run", fake_run)
    assert sch.git_revision() == {"head": "abc", "backend_modified": False}
    assert sch.git_revision() == {"head": "abc", "backend_modified": True}


def test_isolation_record_states_how_each_limit_is_enforced():
    rec = check(IsolationConfig(trusted_environment=True))
    assert set(rec["enforcement"]) >= {"memory_mb", "cpu_seconds", "max_processes", "output_limit_bytes", "wall_clock"}
    if sys.platform.startswith("win"):
        assert all(rec["enforcement"][k].startswith("enforced: Windows job object") for k in ("memory_mb", "cpu_seconds", "max_processes"))


def test_posix_record_does_not_claim_unenforced_limits(monkeypatch):
    monkeypatch.setattr(sys, "platform", "linux")
    e = enforcement(IsolationConfig())
    assert e["max_processes"].startswith("NOT enforced")
    assert "not verified" in e["memory_mb"] and "not verified" in e["cpu_seconds"]
    assert not any(v.startswith("enforced: Windows") for v in e.values())
