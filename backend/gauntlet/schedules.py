"""Shared schedule primitives used by differential testing, exploration, minimization and reporting.

A *schedule* is the JSON document the controller executes:

    {"benchmark_version", "scenario_id", "seed", "initial_orders", "directives", "ops", ...}

Catalog schedules are built by ``scenario.build(seed)`` (``schedule_definition``). *Explicit*
schedules are derived documents (an exploration choice sequence, a minimized candidate, ...) and
additionally carry ``"source": "explicit"`` and ``"derived_from"`` so their hash can never collide
with a catalog schedule hash. Both kinds are executed by the same runner against a fresh database.
"""

from __future__ import annotations

import copy
import hashlib
import inspect
import json
import subprocess
from functools import lru_cache
from pathlib import Path

from gauntlet import BENCHMARK_VERSION
from gauntlet.config import REPO_ROOT

# Logical operation directives that reference an operation by logical id.
OP_DIRECTIVES = ("call", "retry_until_ok", "repeat")
ACTOR_DIRECTIVES = ("call", "run", "run_until", "lose_response", "crash", "restart", "drain",
                    "retry_until_ok", "backfill_until_done", "repeat")


def canonical_hash(obj) -> str:
    return hashlib.sha256(json.dumps(obj, sort_keys=True, separators=(",", ":"), default=str).encode()).hexdigest()


def resolve_scenario(scenario_id: str):
    """Catalog scenario, or a test fixture scenario (fixtures are not part of the benchmark catalog)."""
    from gauntlet.scenarios.catalog import SCENARIOS
    from gauntlet.scenarios.fixtures import FIXTURES
    if scenario_id in SCENARIOS:
        return SCENARIOS[scenario_id]
    if scenario_id in FIXTURES:
        return FIXTURES[scenario_id]
    raise KeyError(f"unknown scenario {scenario_id!r}")


def is_fixture(scenario_id: str) -> bool:
    from gauntlet.scenarios.fixtures import FIXTURES
    return scenario_id in FIXTURES


def build_schedule(scenario_id: str, seed: int) -> dict:
    """The schedule a catalog or fixture scenario defines for a seed."""
    from gauntlet.engine.runner import schedule_definition
    return schedule_definition(scenario_id, seed)


def derive_schedule(base: dict, *, directives: list[dict] | None = None, ops: dict | None = None,
                    derived_from: dict) -> dict:
    """An explicit schedule derived from ``base``; ``derived_from`` records how (kept in the hash)."""
    out = copy.deepcopy(base)
    if directives is not None:
        out["directives"] = copy.deepcopy(directives)
    if ops is not None:
        out["ops"] = copy.deepcopy(ops)
    out["source"] = "explicit"
    out["derived_from"] = {"base_schedule_hash": base.get("derived_from", {}).get("base_schedule_hash")
                           or canonical_hash(base), **derived_from}
    return out


def schedule_id(schedule: dict) -> str:
    return canonical_hash(schedule)


def initial_state_fingerprint(initial_orders: dict) -> str:
    """Hash of the seeded orders plus the harness-owned P0 schema (the full initial database state)."""
    from gauntlet.pg.provision import LEGACY_SCHEMA_SQL
    return canonical_hash({"orders": {str(k): v for k, v in sorted(initial_orders.items(), key=lambda kv: int(kv[0]))},
                           "schema_sql_sha256": hashlib.sha256(LEGACY_SCHEMA_SQL.encode()).hexdigest()})


def _file_sha(path: Path) -> str:
    """SHA-256 of a source file with line endings normalized to LF, so the same commit fingerprints identically
    on checkouts with and without CRLF conversion (git core.autocrlf)."""
    return hashlib.sha256(path.read_bytes().replace(b"\r\n", b"\n")).hexdigest()


def harness_source_fingerprint() -> str:
    """Hash of every Python source file of the harness package (``gauntlet/**/*.py``) as it is on disk now.

    Unlike the git revision, it identifies uncommitted working-tree changes too. Computed on every call."""
    pkg = Path(__file__).resolve().parent
    files = sorted(p for p in pkg.rglob("*.py") if "__pycache__" not in p.parts)
    return canonical_hash({p.relative_to(pkg).as_posix(): _file_sha(p) for p in files})


# The harness source as it was when this process imported it. A long-running process (e.g. the API server)
# keeps executing the code it loaded; if the files change afterwards, runs record that fact.
_SOURCE_AT_PROCESS_START = harness_source_fingerprint()


def harness_source() -> dict:
    now = harness_source_fingerprint()
    return {"fingerprint": now, "fingerprint_at_process_start": _SOURCE_AT_PROCESS_START,
            "changed_since_process_start": now != _SOURCE_AT_PROCESS_START}


@lru_cache(maxsize=None)
def oracle_fingerprint() -> str:
    """Hash of the oracle source files: identifies the oracle version that judged a run."""
    base = Path(__file__).resolve().parent / "oracle"
    return canonical_hash({p.name: _file_sha(p) for p in sorted(base.glob("*.py"))})


@lru_cache(maxsize=None)
def engine_fingerprint() -> str:
    base = Path(__file__).resolve().parent
    files = [base / "engine" / "controller.py", base / "engine" / "runner.py", base / "engine" / "observe.py",
             base / "actor" / "child.py", base / "actor" / "proxy.py", base / "pg" / "provision.py"]
    return canonical_hash({p.name: _file_sha(p) for p in files})


@lru_cache(maxsize=256)
def implementation_fingerprint(impl_id: str) -> dict:
    """Source hashes of an implementation: its class's module plus the base contract and legacy V1."""
    from gauntlet.candidates import is_external
    if is_external(impl_id):  # never imported on the host: identity is the snapshot's canonical source hash
        from gauntlet.candidates import candidate_id_of
        from gauntlet.candidates.snapshots import load_meta
        meta = load_meta(candidate_id_of(impl_id))
        return {"impl_id": impl_id, "class": "external candidate " + meta["manifest"]["entry"]["module"] + "."
                + meta["manifest"]["entry"]["class"], "source_sha256": meta["source_hash"],
                "candidate_id": meta["candidate_id"], "contract_version": meta["contract_version"],
                "loader_version": meta["loader_version"]}
    from gauntlet.impls.registry import get_info
    info = get_info(impl_id)
    files = {Path(inspect.getsourcefile(c)) for c in inspect.getmro(info.cls) if c.__module__.startswith("gauntlet.")}
    files.add(Path(__file__).resolve().parent / "impls" / "legacy.py")
    return {"impl_id": impl_id, "class": f"{info.cls.__module__}.{info.cls.__qualname__}",
            "source_sha256": canonical_hash({p.name: _file_sha(p) for p in sorted(files)})}


def git_revision() -> dict:  # not cached: the working-tree state can change during a long-running process
    """Git HEAD and whether the working tree differs from it (available only inside a git checkout)."""
    try:
        head = subprocess.run(["git", "rev-parse", "HEAD"], cwd=REPO_ROOT, capture_output=True, text=True,
                              timeout=10).stdout.strip()
        dirty = subprocess.run(["git", "status", "--porcelain", "--", "backend"], cwd=REPO_ROOT,
                               capture_output=True, text=True, timeout=10).stdout.strip() != ""
        return {"head": head or None, "backend_modified": dirty if head else None}
    except (OSError, subprocess.SubprocessError):
        return {"head": None, "backend_modified": None}


def provenance(impl_id: str, schedule: dict) -> dict:
    return {
        "schedule_id": schedule_id(schedule),
        "initial_state_fingerprint": initial_state_fingerprint(schedule["initial_orders"]),
        "implementation": implementation_fingerprint(impl_id),
        "oracle_fingerprint": oracle_fingerprint(),
        "engine_fingerprint": engine_fingerprint(),
        "git": git_revision(),
        "harness_source_fingerprint": (src := harness_source())["fingerprint"],
        "harness_source_changed_since_process_start": src["changed_since_process_start"],
    }


def validate_schedule(schedule: dict) -> list[str]:
    """Static checks; returns a list of problems (empty if the schedule is structurally valid).

    Checked: required keys, scenario resolvable, every referenced logical id defined, actors used only
    while spawned and alive (spawn / crash / restart / drain tracked), a non-retry call of a logical id
    at most once, interleave actors running, and explicit choices naming queue actors only.
    Run-time conditions (an actor still busy, a lock wait) can only be detected by executing.
    """
    problems: list[str] = []
    for k in ("scenario_id", "initial_orders", "directives", "ops"):
        if k not in schedule:
            problems.append(f"missing key {k!r}")
    if problems:
        return problems
    try:
        resolve_scenario(schedule["scenario_id"])
    except KeyError as exc:
        problems.append(str(exc))
    ops = schedule["ops"]
    state: dict[str, str] = {}  # actor -> alive | dead | drained
    called: set[str] = set()
    for i, d in enumerate(schedule["directives"]):
        kind = d.get("kind")
        actor = d.get("actor")
        where = f"directive {i} ({kind})"
        if kind == "spawn":
            if state.get(actor) == "alive":
                problems.append(f"{where}: {actor} spawned twice")
            state[actor] = "alive"
            continue
        if kind == "restart":
            if state.get(actor) != "dead":
                problems.append(f"{where}: restart of {actor} which is not crashed")
            state[actor] = "alive"
            continue
        if kind in ACTOR_DIRECTIVES and state.get(actor) != "alive":
            problems.append(f"{where}: actor {actor} is not running")
        if kind == "crash":
            state[actor] = "dead"
        elif kind == "drain":
            state[actor] = "drained"
        if kind in OP_DIRECTIVES:
            lid = d.get("logical_id")
            if lid not in ops:
                problems.append(f"{where}: undefined logical id {lid}")
            if kind == "call" and not d.get("retry"):
                if lid in called:
                    problems.append(f"{where}: {lid} invoked twice without retry")
                called.add(lid)
            if kind in ("retry_until_ok", "repeat") or d.get("retry"):
                if lid not in called:
                    problems.append(f"{where}: {kind} of {lid} before its first call")
        if kind == "interleave":
            for a, q in d.get("queues", {}).items():
                if state.get(a) != "alive":
                    problems.append(f"{where}: actor {a} is not running")
                for lid in q:
                    if lid not in ops:
                        problems.append(f"{where}: undefined logical id {lid}")
                    elif lid in called:
                        problems.append(f"{where}: {lid} invoked twice")
                    called.add(lid)
            for c in d.get("choices") or []:
                if c not in d.get("queues", {}):
                    problems.append(f"{where}: choice {c!r} is not an interleaved actor")
            if d.get("choices") is None and "seed" not in d:
                problems.append(f"{where}: interleave needs either a seed or explicit choices")
    return problems
