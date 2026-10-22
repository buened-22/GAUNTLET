"""Process sandbox and in-process guard (real OS processes; no database).

These tests exercise the actual isolation mechanisms used for candidate actors: the job object /
process group, resource limits, environment scrubbing and the audit-hook guard. They do not claim a
security boundary; they check that each documented control is really applied.
"""

import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

from gauntlet.candidates.sandbox import (ActorSandbox, IsolationConfig, IsolationConfigError, IsolationUnavailable,
                                         capabilities, check, interpreter)

BACKEND = str(Path(__file__).resolve().parents[1])
WIN = sys.platform.startswith("win")
REPO_FILE = Path(__file__).resolve().parents[2] / "README.md"


def run_in_sandbox(code: str, cfg: IsolationConfig | None = None, timeout: float = 60, guard: bool = False):
    sb = ActorSandbox(cfg or IsolationConfig(), pythonpath=BACKEND)
    if guard:
        code = f"from gauntlet.candidates.guard import install\ninstall({str(sb.workspace)!r})\n" + code
    try:
        proc = sb.popen([sb.interpreter, "-s", "-c", code], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        out, err = proc.communicate(timeout=timeout)
        return proc.returncode, out, err, sb.workspace
    finally:
        sb.cleanup()


def test_capabilities_are_reported_honestly():
    caps = capabilities()
    assert caps["strict"]["available"] is False and "not implemented" in caps["strict"]["reason"]
    assert caps["process"]["security_boundary"] is False


def test_strict_isolation_fails_closed():
    with pytest.raises(IsolationUnavailable, match="strict isolation unavailable"):
        check(IsolationConfig(mode="strict"))


def test_isolation_config_validation():
    assert IsolationConfig.from_dict({"memory_mb": 256}).memory_mb == 256
    for bad in ({"mode": "none"}, {"memory_mb": 0}, {"bogus": 1}, {"cpu_seconds": "x"}):
        with pytest.raises(IsolationConfigError):
            IsolationConfig.from_dict(bad)


def test_process_mode_refuses_without_a_trusted_environment_declaration():
    with pytest.raises(IsolationUnavailable, match="not a security boundary"):
        check(IsolationConfig())
    with pytest.raises(IsolationConfigError):
        IsolationConfig.from_dict({"trusted_environment": "yes"})


def test_process_mode_record_states_it_is_not_a_security_boundary():
    rec = check(IsolationConfig(trusted_environment=True))
    assert rec["operator_declared_trusted_environment"] is True
    assert rec["mode"] == "process" and rec["security_boundary"] is False and "NOT a security boundary" in rec["warning"]
    assert any("read isolation" in x for x in rec["not_enforced"]) and any("network" in x for x in rec["not_enforced"])


def test_environment_is_allow_listed(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test-not-real")
    monkeypatch.setenv("GAUNTLET_PG_DSN", "postgresql://u:p@h/db")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "x")
    rc, out, err, ws = run_in_sandbox("import os, json; print(json.dumps(dict(os.environ)))")
    assert rc == 0, err
    env = json.loads(out)
    for k in ("OPENAI_API_KEY", "GAUNTLET_PG_DSN", "AWS_SECRET_ACCESS_KEY", "USERNAME", "APPDATA"):
        assert k not in env, k
    assert env["TEMP"] == str(ws) and env["PYTHONNOUSERSITE"] == "1"


def test_working_directory_is_a_private_workspace():
    rc, out, err, ws = run_in_sandbox("import os; print(os.getcwd())")
    assert rc == 0 and Path(out.strip()) == ws
    assert not ws.exists(), "workspace removed after cleanup"


@pytest.mark.skipif(not WIN, reason="job object limits are Windows-specific (POSIX path untested here)")
def test_child_processes_are_blocked_by_the_job_object():
    code = ("import subprocess, sys\n"
            "try:\n    subprocess.run([sys.executable, '-c', 'pass'], timeout=20)\n    print('SPAWNED')\n"
            "except OSError as e:\n    print('BLOCKED', e.winerror if hasattr(e, 'winerror') else e)\n")
    rc, out, err, _ = run_in_sandbox(code)
    assert "BLOCKED" in out and "SPAWNED" not in out, (out, err)


@pytest.mark.skipif(not WIN, reason="job object limits are Windows-specific (POSIX path untested here)")
def test_memory_limit_is_enforced():
    rc, out, err, _ = run_in_sandbox("b = bytearray(400 * 1024 * 1024)\nprint('ALLOCATED')", IsolationConfig(memory_mb=128))
    assert "ALLOCATED" not in out and ("MemoryError" in err or rc != 0)


@pytest.mark.skipif(not WIN, reason="job object behaviour is Windows-specific (POSIX path untested here)")
def test_terminating_the_job_kills_descendants():
    cfg = IsolationConfig(max_processes=3)  # allow one grandchild for this test only
    sb = ActorSandbox(cfg, pythonpath=BACKEND)
    try:
        code = ("import subprocess, sys, time\n"
                "p = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(300)'])\n"
                "print(p.pid, flush=True)\ntime.sleep(300)\n")
        proc = sb.popen([sb.interpreter, "-s", "-c", code], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        grandchild = int(proc.stdout.readline().strip())
        t = time.monotonic()
        sb.kill_tree(proc)
        assert proc.poll() is not None and time.monotonic() - t < 15
        alive = subprocess.run(["tasklist", "/FI", f"PID eq {grandchild}", "/NH"], capture_output=True, text=True).stdout
        assert str(grandchild) not in alive, "grandchild survived termination of the job"
    finally:
        sb.cleanup()


def test_guard_denies_writes_outside_the_workspace_but_allows_inside():
    before = REPO_FILE.read_bytes()
    code = (f"open(r'{REPO_FILE}', 'a').write('x')\n")
    rc, out, err, _ = run_in_sandbox(code, guard=True)
    assert rc != 0 and "PermissionError" in err and "gauntlet sandbox: write denied" in err
    assert REPO_FILE.read_bytes() == before
    rc, out, err, _ = run_in_sandbox("open('inside.txt', 'w').write('ok'); print(open('inside.txt').read())", guard=True)
    assert rc == 0 and out.strip() == "ok", err


@pytest.mark.parametrize("code,event", [
    ("import os; os.system('echo hi')", "os.system"),
    ("import os; os.remove(r'" + str(REPO_FILE) + "')", "os.remove"),
    ("import os; os.kill(os.getpid(), 9)", "os.kill"),
    ("import socket", "import"),
    ("import ctypes", "import"),
    ("import subprocess", "import"),
])
def test_guard_denies_processes_sockets_ffi_and_deletion(code, event):
    rc, out, err, _ = run_in_sandbox(code, guard=True)
    assert rc != 0 and "gauntlet sandbox:" in err and event in err, err
    assert REPO_FILE.exists()


def test_interpreter_is_a_single_process():
    exe, extra = interpreter()
    assert Path(exe).exists()
    if sys.prefix != sys.base_prefix and WIN:
        assert extra, "venv site-packages must be added when the base interpreter is used"
