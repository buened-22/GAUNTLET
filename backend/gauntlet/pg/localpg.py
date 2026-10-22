"""Manage a local, project-scoped PostgreSQL 16 cluster without Docker.

Binaries are located via ``GAUNTLET_PG_BIN`` or, by default, the portable
binaries from the ``@embedded-postgres/<platform>`` npm package installed under
``.gauntlet/pgdist``. Docker users can ignore this module and point
``GAUNTLET_PG_DSN`` at the docker-compose service instead.
"""

from __future__ import annotations

import os
import platform
import subprocess
import sys
from pathlib import Path

from gauntlet.config import DEFAULT_PG_PORT, GAUNTLET_HOME

PG_NPM_VERSION = "16.14.0-beta.17"  # wraps PostgreSQL 16.14 binaries
DATA_DIR = GAUNTLET_HOME / "pgdata"
LOG_FILE = GAUNTLET_HOME / "postgres.log"
DIST_DIR = GAUNTLET_HOME / "pgdist"


def _platform_pkg() -> str:
    machine = platform.machine().lower()
    arch = "arm64" if machine in ("arm64", "aarch64") else "x64"
    if sys.platform.startswith("win"):
        return f"windows-{arch}"
    if sys.platform == "darwin":
        return f"darwin-{arch}"
    return f"linux-{arch}"


def bin_dir() -> Path:
    env = os.environ.get("GAUNTLET_PG_BIN")
    if env:
        return Path(env)
    return DIST_DIR / "node_modules" / "@embedded-postgres" / _platform_pkg() / "native" / "bin"


def _exe(name: str) -> str:
    suffix = ".exe" if sys.platform.startswith("win") else ""
    path = bin_dir() / f"{name}{suffix}"
    if not path.exists():
        raise FileNotFoundError(
            f"{path} not found. Run `python -m gauntlet pg install` (needs npm) "
            "or set GAUNTLET_PG_BIN to a PostgreSQL 16 bin directory."
        )
    return str(path)


def install() -> None:
    """Download portable PostgreSQL 16 binaries via npm into .gauntlet/pgdist."""
    DIST_DIR.mkdir(parents=True, exist_ok=True)
    npm = "npm.cmd" if sys.platform.startswith("win") else "npm"
    pkg = f"@embedded-postgres/{_platform_pkg()}@{PG_NPM_VERSION}"
    if not (DIST_DIR / "package.json").exists():
        subprocess.run([npm, "init", "-y"], cwd=DIST_DIR, check=True, capture_output=True)
    subprocess.run([npm, "install", pkg, "--no-audit", "--no-fund"], cwd=DIST_DIR, check=True)
    if not sys.platform.startswith("win"):
        hydrate = DIST_DIR / "node_modules" / "@embedded-postgres" / _platform_pkg() / "scripts" / "hydrate-symlinks.js"
        if hydrate.exists():
            subprocess.run(["node", str(hydrate)], cwd=hydrate.parent.parent, check=False)


def init(port: int = DEFAULT_PG_PORT) -> None:
    if (DATA_DIR / "PG_VERSION").exists():
        print(f"cluster already initialised at {DATA_DIR}")
        return
    GAUNTLET_HOME.mkdir(parents=True, exist_ok=True)
    pwfile = GAUNTLET_HOME / "pgpass.tmp"
    pwfile.write_text("gauntlet")
    try:
        subprocess.run(
            [_exe("initdb"), "-D", str(DATA_DIR), "-U", "gauntlet", f"--pwfile={pwfile}",
             "-A", "scram-sha-256", "-E", "UTF8", "--no-locale"],
            check=True, capture_output=True,
        )
    finally:
        pwfile.unlink(missing_ok=True)
    with open(DATA_DIR / "postgresql.conf", "a", encoding="utf-8") as fh:
        fh.write(f"\n# gauntlet\nport = {port}\nlisten_addresses = '127.0.0.1'\nmax_connections = 200\nfsync = on\n")
    print(f"initialised cluster at {DATA_DIR} (port {port})")


def start() -> None:
    subprocess.run([_exe("pg_ctl"), "-D", str(DATA_DIR), "-l", str(LOG_FILE), "-w", "start"], check=True)


def stop() -> None:
    subprocess.run([_exe("pg_ctl"), "-D", str(DATA_DIR), "-m", "fast", "-w", "stop"], check=True)


def status() -> int:
    return subprocess.run([_exe("pg_ctl"), "-D", str(DATA_DIR), "status"]).returncode
