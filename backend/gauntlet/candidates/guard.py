"""In-process guard for candidate actors (Python audit hook, PEP 578).

Installed inside the actor process after the harness's own modules are loaded and before any candidate
code is imported. It denies, by raising PermissionError:

* opening a file for writing, and creating / removing / renaming / chmod-ing paths, outside the actor's
  workspace;
* starting processes (subprocess, os.system, os.exec*, os.spawn*, os.startfile, os.fork) and os.kill;
* socket operations (connect, bind, sendto, getaddrinfo);
* loading native code through ctypes, and importing ctypes / _ctypes / subprocess / socket / _socket /
  multiprocessing / _winapi and the database drivers psycopg / psycopg_c / psycopg_binary / psycopg2.

It is NOT a security boundary, and it is not a control against a deliberately hostile candidate. Its purpose
is to stop *trusted* code from using these facilities by accident.

* Plain Python can switch most of it off. An audit hook cannot be removed once installed, but this one
  reads its rules (``_DENY``, ``_DENY_IMPORTS``, ``_PATH_EVENTS``, ``_WRITE_FLAGS``) from this module's
  globals each time it runs, and candidate code can reach this module through ``sys.modules`` and rebind
  them. A candidate doing so passes static validation. ``tests/test_candidate_sandbox.py`` demonstrates it
  (security audit finding F-02). Moving the rules into the hook's closure would not change this: the hook
  object and its closure cells are reachable from Python too.
* Native code (for example an already-loaded C extension) can bypass it entirely.

What still holds against a candidate that has switched it off are the operating-system limits applied by
``sandbox.py``: on Windows the job object's process-count, memory and CPU-time limits (verified by tests).
The actor process has no database connection at all (the harness owns it, audit finding A-01); denying
the database driver's import here is defense in depth, not isolation.
"""

from __future__ import annotations

import os
import sys

_WRITE_FLAGS = os.O_WRONLY | os.O_RDWR | os.O_APPEND | os.O_CREAT | os.O_TRUNC
_PATH_EVENTS = {"os.remove": 0, "os.rename": 0, "os.replace": 0, "os.rmdir": 0, "os.mkdir": 0, "os.chmod": 0,
                "os.chown": 0, "os.symlink": 0, "os.link": 0, "os.truncate": 0, "os.utime": 0, "shutil.rmtree": 0,
                "shutil.copyfile": 1, "shutil.copytree": 1, "shutil.move": 1, "os.chdir": 0}
_DENY = ("subprocess.Popen", "os.system", "os.exec", "os.posix_spawn", "os.spawn", "os.startfile", "os.fork",
         "os.forkpty", "os.kill", "os.killpg", "_winapi.CreateProcess", "socket.connect", "socket.bind",
         "socket.sendto", "socket.sendmsg", "socket.getaddrinfo", "socket.gethostbyname", "ctypes.dlopen",
         "ctypes.dlsym", "ctypes.call_function", "ctypes.cdata", "sys.setprofile", "sys.settrace",
         "winreg.OpenKey", "winreg.CreateKey", "winreg.SetValue")
_DENY_IMPORTS = ("ctypes", "_ctypes", "subprocess", "socket", "_socket", "multiprocessing", "_winapi",
                 "psycopg", "psycopg_c", "psycopg_binary", "psycopg2")

_installed = False
denied: list[str] = []  # events denied so far (reported in the actor's stderr)


def install(workspace: str) -> None:
    global _installed
    if _installed:
        return
    root = os.path.normcase(os.path.realpath(workspace))

    def inside(path) -> bool:
        if isinstance(path, int):  # an already-open file descriptor
            return True
        try:
            p = os.path.normcase(os.path.realpath(os.fsdecode(path)))
        except (TypeError, ValueError):
            return False
        return p == root or p.startswith(root + os.sep)

    def deny(event: str, detail: str = "") -> None:
        msg = f"gauntlet sandbox: {event} denied {detail}".strip()
        denied.append(msg)
        print(msg, file=sys.stderr)
        raise PermissionError(msg)

    def hook(event: str, args: tuple) -> None:
        if event == "open":
            path, mode, flags = (list(args) + [None, None, None])[:3]
            writing = (isinstance(mode, str) and any(c in mode for c in "wax+")) or \
                      (isinstance(flags, int) and flags & _WRITE_FLAGS)
            if writing and not inside(path):
                deny("write", repr(path))
            return
        if event in _PATH_EVENTS:
            target = args[_PATH_EVENTS[event]] if len(args) > _PATH_EVENTS[event] else None
            if not inside(target):
                deny(event, repr(target))
            return
        if event == "import" and args and args[0] in _DENY_IMPORTS:
            deny("import", args[0])
        if event in _DENY or event.startswith(("os.exec", "os.spawn", "os.posix_spawn")):
            deny(event)

    sys.addaudithook(hook)
    _installed = True
