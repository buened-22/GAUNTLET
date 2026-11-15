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


def test_process_mode_record_lists_the_audit_controls_and_their_limits():
    """The isolation record (rendered as "Applied" / "Not enforced" in every candidate evaluation summary) names
    the A-01 and A-02 controls and the gaps they leave, and each claimed control exists in the implementation.
    End-to-end enforcement is tested in test_audit_hardening_pg.py (A-01) and test_observer_role_pg.py (A-02)."""
    import ast
    from gauntlet.candidates import guard
    from gauntlet.engine import runner
    from gauntlet.pg import provision

    rec = check(IsolationConfig(trusted_environment=True))
    controls, gaps = " | ".join(rec["controls"]), " | ".join(rec["not_enforced"])
    # A-01: the actor has no database access; the harness executes one granted statement at a time
    assert "harness-owned database connection (A-01)" in controls and "is given no connection, DSN or credentials" in controls
    child = (Path(BACKEND) / "gauntlet" / "actor" / "child.py").read_text(encoding="utf-8")
    imported = {a.name.split(".")[0] for n in ast.walk(ast.parse(child)) if isinstance(n, ast.Import) for a in n.names}
    imported |= {(n.module or "").split(".")[0] for n in ast.walk(ast.parse(child)) if isinstance(n, ast.ImportFrom)}
    assert "psycopg" not in imported, "the actor process must not import the database driver"
    # the guard claim, including the database driver, matches the guard's deny list, and the record says the
    # guard is not a control (plain Python can switch it off; audit finding F-02)
    assert "tripwire for trusted code, not a control" in controls and "plain Python can switch it off" in controls
    assert "no database-driver imports" in controls and "Python level only" not in controls
    assert {"psycopg", "psycopg2"} <= set(guard._DENY_IMPORTS)
    # A-02: non-superuser observer, control connection outside the run database
    assert "non-superuser observer role (A-02)" in controls and "no superuser session in the run database" in controls
    assert "admin-role connection" in controls and "never to the run database" in controls  # privileged, stated
    assert callable(runner.open_observer) and callable(runner.open_control)
    import inspect
    role_sql = inspect.getsource(provision.create_observer_role)
    assert all(attr in role_sql for attr in ("NOSUPERUSER", "NOCREATEDB", "NOCREATEROLE", "NOBYPASSRLS", "NOINHERIT"))
    # the limits these controls leave are stated, not hidden
    assert "environment and memory" in gaps and "new database connection opened by candidate code" in gaps
    assert "disabling the audit-hook guard (plain Python suffices)" in gaps
    assert "native code defeating" not in gaps  # the old wording implied only native code could bypass it
    # nothing in the record claims a boundary or an available strict mode
    assert rec["security_boundary"] is False
    assert not any("security boundary" in c.lower() or "container" in c.lower() or "strict" in c.lower()
                   for c in rec["controls"])


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


# -- audit finding F-02: what the guard is, and what it is not --------------------------------------------
# The guard is documented as a tripwire for trusted code, NOT a security control: plain Python can switch most
# of it off. These tests pin that documented behaviour. If the guard is ever hardened so that they fail, the
# documentation (SECURITY.md, docs/SECURITY_MODEL.md, guard.py, the isolation record) must be updated with them.
REBIND_GUARD = ("import sys\n"
                "g = sys.modules['gauntlet.candidates.guard']\n"
                "g._DENY = (); g._DENY_IMPORTS = (); g._PATH_EVENTS = {}; g._WRITE_FLAGS = 0\n")


def test_plain_python_can_switch_the_guard_off(tmp_path):
    """With the guard intact a socket import and a write outside the workspace are denied; after three lines of
    ordinary Python (no native code) the same actor connects to a loopback listener, creates a file outside its
    workspace and imports ctypes. This is why the guard is not a security control."""
    import socket
    import threading
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen(1)
    port = listener.getsockname()[1]
    accepted = []
    threading.Thread(target=lambda: accepted.append(listener.accept()[0]), daemon=True).start()
    outside = tmp_path / "written-outside-the-workspace.txt"
    try:
        rc, out, err, _ = run_in_sandbox("import socket", guard=True)
        assert rc != 0 and "import denied socket" in err, err
        rc, out, err, _ = run_in_sandbox(f"import os; os.close(os.open({str(outside)!r}, os.O_WRONLY | os.O_CREAT))",
                                         guard=True)
        assert rc != 0 and "write denied" in err and not outside.exists(), err
        code = (REBIND_GUARD +
                f"import socket; s = socket.create_connection(('127.0.0.1', {port}), timeout=10); s.close()\n"
                f"import os; os.close(os.open({str(outside)!r}, os.O_WRONLY | os.O_CREAT))\n"
                "import ctypes\nprint('GUARD-OFF')")
        rc, out, err, _ = run_in_sandbox(code, guard=True)
        assert rc == 0 and "GUARD-OFF" in out, err
        assert outside.exists() and accepted, "the actor wrote outside its workspace and reached the listener"
    finally:
        listener.close()
        for s in accepted:
            s.close()


def test_the_mode_check_for_open_is_not_rebindable():
    """Precision about the bypass: open(path, 'w') outside the workspace stays denied after rebinding, because
    the hook checks the mode string itself rather than a module global. (os.open, above, is not.)"""
    rc, out, err, _ = run_in_sandbox(REBIND_GUARD + f"open({str(REPO_FILE)!r}, 'a').write('x')", guard=True)
    assert rc != 0 and "write denied" in err, err


@pytest.mark.skipif(not WIN, reason="job object limits are Windows-specific (POSIX path untested here)")
def test_os_level_limits_still_hold_after_the_guard_is_switched_off():
    """What does hold against a candidate that has switched the guard off: the operating-system limits."""
    code = (REBIND_GUARD + "import subprocess, sys\n"
            "try:\n    subprocess.run([sys.executable, '-c', 'pass'], timeout=20)\n    print('SPAWNED')\n"
            "except OSError as e:\n    print('BLOCKED', getattr(e, 'winerror', e))\n")
    rc, out, err, _ = run_in_sandbox(code, guard=True)
    assert "BLOCKED" in out and "SPAWNED" not in out, (out, err)
    rc, out, err, _ = run_in_sandbox(REBIND_GUARD + "b = bytearray(300 * 1024 * 1024)\nprint('ALLOCATED')",
                                     IsolationConfig(memory_mb=150), guard=True)
    assert "ALLOCATED" not in out and ("MemoryError" in err or rc != 0), (out, err)


def test_interpreter_is_a_single_process():
    exe, extra = interpreter()
    assert Path(exe).exists()
    if sys.prefix != sys.base_prefix and WIN:
        assert extra, "venv site-packages must be added when the base interpreter is used"
