"""Isolation for external candidate actor processes.

Modes
-----
``strict``   Container isolation (separate file system, no network, resource quotas). Execution in a
             container is NOT implemented in this version, so this mode always fails closed with
             ``IsolationUnavailable``; it never silently degrades.
``process``  Development-grade process sandbox. NOT a security boundary. Applied controls:

  * every actor process runs with an allow-listed environment (no API keys, tokens, database
    credentials or other host variables), ``-s`` (no user site-packages), its own empty temporary
    working directory (also ``TEMP``/``TMP``), and ``PYTHONDONTWRITEBYTECODE``;
  * Windows: the process is placed in a Job Object with kill-on-close, an active-process limit of 1
    (it cannot start child processes), a per-process memory limit and a per-process CPU-time limit;
    terminating the job kills the process and any descendants. POSIX: a new session (process group),
    RLIMIT_AS and RLIMIT_CPU, and the whole group is killed on termination; the process-count limit is NOT
    enforced on POSIX (this path has not been exercised in the development environment);
  * inside the actor, before candidate code is imported, a Python audit-hook guard (``guard.py``)
    denies writes outside the workspace, process creation, socket operations and FFI (ctypes);
  * the actor connects to PostgreSQL as a per-run role that owns only its run database (not a
    superuser, no CREATEDB / CREATEROLE), with CONNECT revoked from PUBLIC on that database.

  Not enforced: OS-level file-system read isolation (a candidate can read files the host user can
  read, including this repository), OS-level network blocking (native code can still open sockets),
  and protection against a candidate that defeats the audit hook from native code. Use ``strict``
  (once implemented) or an external VM for genuinely untrusted code.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
import threading
from dataclasses import asdict, dataclass, field
from pathlib import Path

DEFAULTS = {
    "memory_mb": 512,          # per actor process
    "cpu_seconds": 120,        # per actor process (CPU time, not wall clock)
    "max_processes": 1,        # active processes per actor job (1 = cannot spawn children)
    "output_limit_bytes": 1_000_000,  # stderr captured per actor; the rest is drained and counted
    "max_concurrent_runs": 2,  # candidate runs executing at the same time (harness-wide)
}
ENV_ALLOWLIST = ("SYSTEMROOT", "WINDIR", "COMSPEC", "PATHEXT", "NUMBER_OF_PROCESSORS", "PROCESSOR_ARCHITECTURE",
                 "LANG", "LC_ALL", "TZ")


class IsolationConfigError(ValueError):
    """An isolation configuration is malformed."""


class IsolationUnavailable(RuntimeError):
    """The requested isolation mode cannot be provided here; the run must not start (fail closed)."""


@dataclass
class IsolationConfig:
    mode: str = "process"
    memory_mb: int = DEFAULTS["memory_mb"]
    cpu_seconds: int = DEFAULTS["cpu_seconds"]
    max_processes: int = DEFAULTS["max_processes"]
    output_limit_bytes: int = DEFAULTS["output_limit_bytes"]
    # process mode is not a security boundary: it only runs when the operator explicitly declares the host a
    # trusted, disposable development environment (fail closed otherwise)
    trusted_environment: bool = False

    @classmethod
    def from_dict(cls, d: dict | None) -> "IsolationConfig":
        d = dict(d or {})
        unknown = set(d) - set(cls.__dataclass_fields__)
        if unknown:
            raise IsolationConfigError(f"unknown isolation options {sorted(unknown)}")
        try:
            c = cls(**d)
        except TypeError as exc:
            raise IsolationConfigError(str(exc)) from None
        if c.mode not in ("process", "strict"):
            raise IsolationConfigError("isolation mode must be 'process' or 'strict'")
        for k in ("memory_mb", "cpu_seconds", "max_processes", "output_limit_bytes"):
            if not isinstance(getattr(c, k), int) or isinstance(getattr(c, k), bool) or getattr(c, k) < 1:
                raise IsolationConfigError(f"{k} must be a positive integer")
        if not isinstance(c.trusted_environment, bool):
            raise IsolationConfigError("trusted_environment must be true or false")
        return c

    def to_dict(self) -> dict:
        return asdict(self)


def capabilities() -> dict:
    """What this host can actually provide (reported with every candidate run)."""
    runtime = shutil.which("docker") or shutil.which("podman")
    win = sys.platform.startswith("win")
    job = False
    if win:
        try:
            import ctypes  # noqa: F401
            job = True
        except ImportError:
            job = False
    return {
        "platform": sys.platform,
        "strict": {"available": False,
                   "reason": ("container execution is not implemented in this version"
                              if runtime else "no container runtime (docker/podman) found and container "
                              "execution is not implemented in this version")},
        "process": {"available": True, "security_boundary": False,
                    "process_tree_control": "windows-job-object" if job else ("posix-process-group" if not win else "none"),
                    "resource_limits": "job object (memory, cpu time, process count)" if job
                    else ("rlimit (address space, cpu time); process count not enforced" if not win else "none")},
    }


def check(config: IsolationConfig) -> dict:
    """Fail closed if the requested mode is not available; return the isolation record for the run."""
    caps = capabilities()
    if config.mode == "strict":
        raise IsolationUnavailable(f"strict isolation unavailable: {caps['strict']['reason']}")
    if not config.trusted_environment:
        raise IsolationUnavailable(
            "process isolation is not a security boundary; refusing to execute a candidate unless the operator "
            "declares this host a trusted, disposable development environment (CLI --trusted-dev-env, API/config "
            "isolation.trusted_environment = true). Never use it for untrusted code.")
    return {"mode": "process", "security_boundary": False, "config": config.to_dict(),
            "enforcement": enforcement(config),
            "operator_declared_trusted_environment": True,
            "controls": ["allow-listed environment (no host secrets)", "per-actor temporary workspace",
                         caps["process"]["process_tree_control"], caps["process"]["resource_limits"],
                         "audit-hook guard: no writes outside workspace, no process creation, no sockets, no ctypes",
                         "per-run PostgreSQL role (owner of its run database only; not superuser)"],
            "not_enforced": ["OS-level read isolation (host files readable by the host user remain readable)",
                             "OS-level network isolation (native code can open sockets)",
                             "protection against native code defeating the audit-hook guard"],
            "warning": "process mode is a development sandbox, NOT a security boundary"}


def enforcement(config: IsolationConfig) -> dict:
    """How each configured limit is enforced on this platform. Recorded with every run so that a requested
    limit is never reported as enforced when it is not."""
    if sys.platform.startswith("win"):
        job = "enforced: Windows job object ({}); verified by tests on this platform"
        return {"memory_mb": job.format("per-process memory limit"),
                "cpu_seconds": job.format("per-process user CPU-time limit"),
                "max_processes": job.format("active-process limit"),
                "output_limit_bytes": "enforced by the harness: stderr beyond the limit is drained and counted, not stored",
                "wall_clock": "enforced by the harness run deadline and watchdog (not by this sandbox)"}
    return {"memory_mb": "requested via RLIMIT_AS (POSIX path not verified in the development environment)",
            "cpu_seconds": "requested via RLIMIT_CPU (POSIX path not verified in the development environment)",
            "max_processes": "NOT enforced on POSIX in this version",
            "output_limit_bytes": "enforced by the harness: stderr beyond the limit is drained and counted, not stored",
            "wall_clock": "enforced by the harness run deadline and watchdog (not by this sandbox)"}


# -- Windows job objects ---------------------------------------------------------------------------
if sys.platform.startswith("win"):
    import ctypes
    from ctypes import wintypes

    _k32 = ctypes.WinDLL("kernel32", use_last_error=True)

    class _IO_COUNTERS(ctypes.Structure):
        _fields_ = [(n, ctypes.c_ulonglong) for n in ("ReadOperationCount", "WriteOperationCount", "OtherOperationCount",
                                                      "ReadTransferCount", "WriteTransferCount", "OtherTransferCount")]

    class _BASIC(ctypes.Structure):
        _fields_ = [("PerProcessUserTimeLimit", ctypes.c_longlong), ("PerJobUserTimeLimit", ctypes.c_longlong),
                    ("LimitFlags", wintypes.DWORD), ("MinimumWorkingSetSize", ctypes.c_size_t),
                    ("MaximumWorkingSetSize", ctypes.c_size_t), ("ActiveProcessLimit", wintypes.DWORD),
                    ("Affinity", ctypes.c_size_t), ("PriorityClass", wintypes.DWORD), ("SchedulingClass", wintypes.DWORD)]

    class _EXTENDED(ctypes.Structure):
        _fields_ = [("BasicLimitInformation", _BASIC), ("IoInfo", _IO_COUNTERS),
                    ("ProcessMemoryLimit", ctypes.c_size_t), ("JobMemoryLimit", ctypes.c_size_t),
                    ("PeakProcessMemoryUsed", ctypes.c_size_t), ("PeakJobMemoryUsed", ctypes.c_size_t)]

    _k32.CreateJobObjectW.restype = wintypes.HANDLE
    _k32.CreateJobObjectW.argtypes = [wintypes.LPVOID, wintypes.LPCWSTR]
    _k32.SetInformationJobObject.argtypes = [wintypes.HANDLE, ctypes.c_int, wintypes.LPVOID, wintypes.DWORD]
    _k32.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
    _k32.TerminateJobObject.argtypes = [wintypes.HANDLE, wintypes.UINT]
    _k32.CloseHandle.argtypes = [wintypes.HANDLE]
    _k32.OpenProcess.restype = wintypes.HANDLE
    _k32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]

    _LIMIT_PROCESS_TIME, _LIMIT_ACTIVE_PROCESS, _LIMIT_PROCESS_MEMORY = 0x2, 0x8, 0x100
    _LIMIT_DIE_ON_UNHANDLED_EXCEPTION, _LIMIT_KILL_ON_JOB_CLOSE = 0x400, 0x2000
    _EXTENDED_LIMIT_INFORMATION = 9

    class _Job:
        def __init__(self, cfg: IsolationConfig):
            self.handle = _k32.CreateJobObjectW(None, None)
            if not self.handle:
                raise OSError(ctypes.get_last_error(), "CreateJobObjectW failed")
            info = _EXTENDED()
            b = info.BasicLimitInformation
            b.LimitFlags = (_LIMIT_KILL_ON_JOB_CLOSE | _LIMIT_ACTIVE_PROCESS | _LIMIT_PROCESS_MEMORY
                            | _LIMIT_PROCESS_TIME | _LIMIT_DIE_ON_UNHANDLED_EXCEPTION)
            b.ActiveProcessLimit = cfg.max_processes
            b.PerProcessUserTimeLimit = cfg.cpu_seconds * 10_000_000  # 100 ns units
            info.ProcessMemoryLimit = cfg.memory_mb * 1024 * 1024
            if not _k32.SetInformationJobObject(self.handle, _EXTENDED_LIMIT_INFORMATION, ctypes.byref(info),
                                                ctypes.sizeof(info)):
                err = ctypes.get_last_error()
                self.close()
                raise OSError(err, "SetInformationJobObject failed")

        def assign(self, pid: int) -> None:
            h = _k32.OpenProcess(0x0001 | 0x0100, False, pid)  # PROCESS_TERMINATE | PROCESS_SET_QUOTA
            if not h:
                raise OSError(ctypes.get_last_error(), "OpenProcess failed")
            try:
                if not _k32.AssignProcessToJobObject(self.handle, h):
                    raise OSError(ctypes.get_last_error(), "AssignProcessToJobObject failed")
            finally:
                _k32.CloseHandle(h)

        def terminate(self) -> None:
            if self.handle:
                _k32.TerminateJobObject(self.handle, 1)

        def close(self) -> None:
            if self.handle:
                _k32.CloseHandle(self.handle)  # kill-on-close: anything still in the job dies
                self.handle = None


# -- per-actor sandbox -----------------------------------------------------------------------------
def interpreter() -> tuple[str, list[str]]:
    """The interpreter to run actors with, and extra import paths.

    In a virtual environment on Windows, ``sys.executable`` is a launcher that starts the base interpreter as
    a child process, which the job object's process limit (correctly) forbids. Actors are therefore started
    with the base interpreter directly and the environment's site-packages on PYTHONPATH.
    """
    import site
    base = getattr(sys, "_base_executable", sys.executable)
    if sys.prefix != sys.base_prefix and base and os.path.exists(base) and base != sys.executable:
        return base, [p for p in site.getsitepackages() if os.path.isdir(p)]
    return sys.executable, []


class ActorSandbox:
    """Workspace, environment, process-tree control and limits for ONE actor process."""

    def __init__(self, cfg: IsolationConfig, pythonpath: str):
        self.cfg = cfg
        self.workspace = Path(tempfile.mkdtemp(prefix="gauntlet-cand-"))
        self.interpreter, extra = interpreter()
        self._pythonpath = os.pathsep.join([pythonpath, *extra])
        self._job = None
        self.events: list[str] = []

    def env(self) -> dict:
        env = {k: os.environ[k] for k in ENV_ALLOWLIST if k in os.environ}
        env.update({"PATH": os.path.dirname(sys.executable), "PYTHONPATH": self._pythonpath, "PYTHONUNBUFFERED": "1",
                    "PYTHONDONTWRITEBYTECODE": "1", "PYTHONNOUSERSITE": "1", "PYTHONHASHSEED": "0",
                    "TEMP": str(self.workspace), "TMP": str(self.workspace), "TMPDIR": str(self.workspace),
                    "HOME": str(self.workspace), "USERPROFILE": str(self.workspace)})
        return env

    def popen(self, argv: list[str], **kw) -> subprocess.Popen:
        if sys.platform.startswith("win"):
            proc = subprocess.Popen(argv, env=self.env(), cwd=self.workspace, **kw)
            try:
                self._job = _Job(self.cfg)
                self._job.assign(proc.pid)
            except OSError as exc:  # fail closed: never run a candidate actor outside its job
                proc.kill()
                raise IsolationUnavailable(f"could not place the actor in a job object: {exc}") from exc
            return proc
        cfg = self.cfg

        def limits():  # pragma: no cover - POSIX only
            import resource
            os.setsid()
            mem = cfg.memory_mb * 1024 * 1024
            resource.setrlimit(resource.RLIMIT_AS, (mem, mem))
            resource.setrlimit(resource.RLIMIT_CPU, (cfg.cpu_seconds, cfg.cpu_seconds))
        return subprocess.Popen(argv, env=self.env(), cwd=self.workspace, preexec_fn=limits, **kw)

    def kill_tree(self, proc: subprocess.Popen) -> None:
        if sys.platform.startswith("win"):
            if self._job:
                self._job.terminate()
        else:  # pragma: no cover - POSIX only
            try:
                os.killpg(proc.pid, 9)
            except OSError:
                pass
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()

    def cleanup(self) -> None:
        if self._job:
            self._job.close()
            self._job = None
        shutil.rmtree(self.workspace, ignore_errors=True)


_slots_lock = threading.Lock()
_slots: threading.BoundedSemaphore | None = None


def run_slot(timeout: float = 600) -> threading.BoundedSemaphore:
    """Harness-wide limit on concurrently executing candidate runs."""
    global _slots
    with _slots_lock:
        if _slots is None:
            _slots = threading.BoundedSemaphore(int(os.environ.get("GAUNTLET_MAX_CANDIDATE_RUNS",
                                                                   DEFAULTS["max_concurrent_runs"])))
    if not _slots.acquire(timeout=timeout):
        raise IsolationUnavailable(f"no candidate execution slot became free within {timeout}s")
    return _slots


def field_names() -> list[str]:  # used by API validation
    return [f for f in IsolationConfig.__dataclass_fields__]


__all__ = ["ActorSandbox", "IsolationConfig", "IsolationUnavailable", "capabilities", "check", "run_slot", "field"]
