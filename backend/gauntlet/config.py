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
# When a statement exceeds STEP_TIMEOUT_S the harness gathers evidence before classifying it (see
# gauntlet.actor.proxy.step_timeout_attribution): how long it waits for a cancel request to end the statement,
# and the longest gap between two iterations of its own wait loop that still counts as "the harness did not stall".
STEP_CANCEL_GRACE_S = float(os.environ.get("GAUNTLET_STEP_CANCEL_GRACE_S", "5"))
STEP_STALL_GAP_S = float(os.environ.get("GAUNTLET_STEP_STALL_GAP_S", "2"))
CHILD_LOCK_TIMEOUT_MS = int(os.environ.get("GAUNTLET_LOCK_TIMEOUT_MS", "15000"))
# The harness's monitor connection reads snapshots while actors are paused at gates. An actor paused while
# holding a lock on the observed tables can never release it (the controller is single-threaded), so a
# monitor lock wait is a deadlock: bound it and report an infrastructure error.
MONITOR_LOCK_TIMEOUT_MS = int(os.environ.get("GAUNTLET_MONITOR_LOCK_TIMEOUT_MS", "15000"))
# The harness executes actor statements in its own process: bound what one statement may return (rows, and
# bytes of the encoded result sent to the actor), and how long opening an actor's connection may take.
MAX_RESULT_ROWS = int(os.environ.get("GAUNTLET_MAX_RESULT_ROWS", "100000"))
MAX_RESULT_BYTES = int(os.environ.get("GAUNTLET_MAX_RESULT_BYTES", str(16 * 1024 * 1024)))
ACTOR_CONNECT_TIMEOUT_S = int(os.environ.get("GAUNTLET_ACTOR_CONNECT_TIMEOUT_S", "10"))
