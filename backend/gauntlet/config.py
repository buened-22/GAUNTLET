"""Runtime configuration, read from environment variables with local defaults."""

from __future__ import annotations

import os
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
GAUNTLET_HOME = Path(os.environ.get("GAUNTLET_HOME", REPO_ROOT / ".gauntlet"))

# Admin DSN for the PostgreSQL 16 server used by integration runs. Each run
# creates (and drops) its own database on this server.
DEFAULT_PG_PORT = 55432
PG_ADMIN_DSN = os.environ.get(
    "GAUNTLET_PG_DSN",
    f"postgresql://gauntlet:gauntlet@127.0.0.1:{DEFAULT_PG_PORT}/postgres",
)

RESULTS_DB = Path(os.environ.get("GAUNTLET_RESULTS_DB", GAUNTLET_HOME / "results.sqlite3"))

# Safety nets. These bound how long the harness waits; they are not part of
# the logical schedule. A run that hits one is reported as an infrastructure
# error or inconclusive, never as a pass.
STEP_TIMEOUT_S = float(os.environ.get("GAUNTLET_STEP_TIMEOUT_S", "20"))
CHILD_LOCK_TIMEOUT_MS = int(os.environ.get("GAUNTLET_LOCK_TIMEOUT_MS", "15000"))
