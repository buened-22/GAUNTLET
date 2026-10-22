"""External candidates end to end on PostgreSQL: adversarial behaviour, integration with the existing
engines, the evaluation workflow, cleanup. Candidates here are SYNTHETIC fixtures, not AI-generated."""

import glob
import hashlib
import json
import os
import shutil
import subprocess
import tempfile
import time
from pathlib import Path

import psycopg
import pytest

from gauntlet.candidates.evaluate import evaluate
from gauntlet.candidates.snapshots import register
from gauntlet.config import PG_ADMIN_DSN
from gauntlet.differential import run_differential
from gauntlet.engine.runner import execute_run
from gauntlet.minimize import minimize
from gauntlet.store import Store

pytestmark = pytest.mark.pg
HERE = Path(__file__).resolve().parent
GOOD = HERE / "candidates" / "synthetic_ref_a_copy"
BAD = HERE / "candidates" / "synthetic_stale_backfill"
REPO = HERE.parents[1]
F01, S02 = "F01_backfill_race_probe", "S02_backfill_vs_legacy_writes"
# process isolation is not a security boundary: these trusted synthetic fixtures run with the explicit
# trusted-environment declaration the harness requires
ISO = {"trusted_environment": True}


@pytest.fixture(scope="module")
def env(tmp_path_factory, pg_available):
    root = tmp_path_factory.mktemp("cands")
    old = os.environ.get("GAUNTLET_CANDIDATES_DIR")
    os.environ["GAUNTLET_CANDIDATES_DIR"] = str(root / "snapshots")
    ws_before = set(glob.glob(os.path.join(tempfile.gettempdir(), "gauntlet-cand-*")))
    yield {"root": root, "store": Store(root / "results.sqlite3")}
    if old is None:
        os.environ.pop("GAUNTLET_CANDIDATES_DIR", None)
    else:
        os.environ["GAUNTLET_CANDIDATES_DIR"] = old
    # nothing may be left behind by any exit path exercised in this module
    with psycopg.connect(PG_ADMIN_DSN, autocommit=True) as c:
        dbs = [r[0] for r in c.execute("SELECT datname FROM pg_database WHERE datname LIKE 'gauntlet_run_%'")]
        roles = [r[0] for r in c.execute("SELECT rolname FROM pg_roles WHERE rolname LIKE 'gauntlet_cand_%'")]
    assert dbs == [] and roles == [], (dbs, roles)
    assert set(glob.glob(os.path.join(tempfile.gettempdir(), "gauntlet-cand-*"))) <= ws_before


def misbehaving(env, name: str, body: str, imports: str = "import os, sys") -> str:
    d = env["root"] / name
    shutil.copytree(GOOD, d)
    src = (d / "candidate.py").read_text(encoding="utf-8")
    src = src.replace("from __future__ import annotations\n", f"from __future__ import annotations\n\n{imports}\n", 1)
    src += "\n\nclass Misbehaving(Implementation):\n    def migrator_expand(self, db, a):\n" + \
           "\n".join("        " + line for line in body.strip().splitlines()) + "\n"
    (d / "candidate.py").write_text(src, encoding="utf-8")
    m = json.loads((d / "gauntlet_candidate.json").read_text(encoding="utf-8"))
    m["name"], m["entry"]["class"] = name.replace("_", "-"), "Misbehaving"
    (d / "gauntlet_candidate.json").write_text(json.dumps(m), encoding="utf-8")
    return register(d)["impl_id"]


def attempt(run, op):
    return next(a for a in run["attempts"] if a["op"] == op)


# -- adversarial candidates --------------------------------------------------------------------------
def test_candidate_exception_is_a_client_visible_failure_not_a_crash(env):
    r = execute_run(F01, misbehaving(env, "raises", "raise RuntimeError('boom')"), 1, time_limit_s=120, isolation=ISO)
    assert r["error_class"] is None and r["infra_error"] is None
    e = attempt(r, "expand")
    assert e["status"] == "failed" and e["error"]["code"] == "RuntimeError"
    assert r["verdict"] == "fail", "a migration that never expands cannot reach a valid terminal state"


def test_candidate_process_exit_is_a_candidate_execution_failure(env):
    r = execute_run(F01, misbehaving(env, "exits", "os._exit(3)"), 1, time_limit_s=120, isolation=ISO)
    assert r["verdict"] == "infra_error" and r["error_class"] == "candidate_execution_failure"
    assert all(i["status"] == "infra_error" for i in r["invariants"])


def test_candidate_that_fails_to_load_is_a_candidate_execution_failure(env):
    impl = misbehaving(env, "bad_import", "return {}", imports="import os, sys\nraise ImportError('broken at import')")
    r = execute_run(F01, impl, 1, time_limit_s=120, isolation=ISO)
    assert r["error_class"] == "candidate_execution_failure" and "could not load implementation" in r["infra_error"]


def test_endless_candidate_is_stopped_by_the_timeout(env):
    t = time.monotonic()
    r = execute_run(F01, misbehaving(env, "spins", "while True:\n    pass"), 1, time_limit_s=6, isolation=ISO)
    assert r["error_class"] == "timeout" and r["verdict"] == "infra_error"
    assert time.monotonic() - t < 40


def test_candidate_cannot_start_processes(env):
    r = execute_run(F01, misbehaving(env, "spawns", "os.system('echo pwned')\nreturn {}"), 1, time_limit_s=120, isolation=ISO)
    e = attempt(r, "expand")
    assert e["status"] == "failed" and e["error"]["code"] == "PermissionError"
    assert "os.system denied" in " ".join(r["actor_output"]["migrator"]["tail"])


def test_candidate_cannot_modify_protected_files(env):
    target = REPO / "evidence" / "results-bench-eed0c47427.sqlite3.sha256"
    before = target.read_bytes()
    body = f"open(r'{target}', 'a').write('tampered')\nreturn {{}}"
    r = execute_run(F01, misbehaving(env, "writes", body), 1, time_limit_s=120, isolation=ISO)
    assert attempt(r, "expand")["error"]["code"] == "PermissionError"
    assert target.read_bytes() == before


def test_candidate_sees_no_host_secrets(env, monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test-not-real")
    monkeypatch.setenv("GAUNTLET_PG_DSN", PG_ADMIN_DSN)
    r = execute_run(F01, misbehaving(env, "env_probe", "res = super().migrator_expand(db, a)\n"
                                                        "res['env'] = sorted(os.environ)\nreturn res"), 1, time_limit_s=120, isolation=ISO)
    keys = attempt(r, "expand")["result"]["env"]
    assert "OPENAI_API_KEY" not in keys and "GAUNTLET_PG_DSN" not in keys and "PGPASSWORD" not in keys


def test_candidate_database_role_is_not_privileged(env):
    body = ("out = []\n"
            "for sql in ('CREATE DATABASE gauntlet_evil', 'ALTER ROLE CURRENT_USER SUPERUSER',\n"
            "            \"COPY (SELECT 1) TO PROGRAM 'echo pwned'\", 'SELECT * FROM pg_authid'):\n"
            "    try:\n        db.execute(sql)\n        out.append('ALLOWED ' + sql)\n"
            "    except Exception as exc:\n        out.append('denied: ' + str(exc).splitlines()[0])\n"
            "res = super().migrator_expand(db, a)\nres['probe'] = out\nreturn res")
    r = execute_run(F01, misbehaving(env, "privs", body), 1, time_limit_s=120, isolation=ISO)
    probe = attempt(r, "expand")["result"]["probe"]
    assert not any(p.startswith("ALLOWED") for p in probe), probe
    assert all("permission denied" in p.lower() for p in probe), probe
    assert r["database_role"]["superuser"] is False


def test_output_flood_is_capped(env):
    r = execute_run(F01, misbehaving(env, "floods", "sys.stderr.write('x' * 3_000_000 + '\\n')\n"
                                                     "return super().migrator_expand(db, a)"), 1, time_limit_s=120, isolation=ISO)
    out = r["actor_output"]["migrator"]
    assert out["truncated"] and out["stderr_bytes"] >= 3_000_000 and r["error_class"] is None


def test_strict_isolation_fails_closed_without_executing(env):
    r = execute_run(F01, register(GOOD)["impl_id"], 1, isolation={"mode": "strict"})
    assert r["error_class"] == "isolation_unavailable" and r["database"] is None and r["events"] == []


# -- integration with the existing engines -----------------------------------------------------------
def test_differential_agreement_and_confirmed_violation(env):
    st = env["store"]
    good, bad = register(GOOD)["impl_id"], register(BAD)["impl_id"]
    agree = run_differential(st, ["ref_a_trigger", good], S02, 1, isolation=ISO)["result"]
    assert agree["classification"] == "agreement", agree["reason"]
    viol = run_differential(st, ["ref_a_trigger", bad], S02, 1, isolation=ISO)["result"]
    assert viol["classification"] == "confirmed_violation"
    assert "S1_ACKED_DURABLE" in viol["oracle"][bad]["failed_invariants"]
    run = st.get_run(viol["oracle"][bad]["run_id"])
    assert run["provenance"]["implementation"]["source_sha256"] == register(BAD)["source_hash"]
    assert run["isolation"]["mode"] == "process" and run["candidate"]["candidate_id"] == register(BAD)["candidate_id"]
    env["bad_run"] = run["id"]


def test_candidate_failure_can_be_minimized(env):
    st = env["store"]
    rid = env.get("bad_run") or run_differential(st, ["ref_a_trigger", register(BAD)["impl_id"]], S02, 1, isolation=ISO)["result"]["oracle"][register(BAD)["impl_id"]]["run_id"]
    r = minimize(st, rid, invariant="S1_ACKED_DURABLE", budget={"max_attempts": 8})["result"]
    assert r["original_unchanged"] and r["minimized"]["replay"]["signature_match"]
    assert r["reduction"]["directives"][1] < r["reduction"]["directives"][0]


def test_evaluation_workflow_end_to_end_and_no_overwrite(env):
    st = env["store"]
    frozen = REPO / "evidence" / "results-bench-eed0c47427.sqlite3"
    frozen_sha = hashlib.sha256(frozen.read_bytes()).hexdigest()
    cid = register(BAD)["candidate_id"]
    kw = dict(scenarios=[S02], seeds=[1], references=["ref_a_trigger"], run_time_limit_s=120, isolation=ISO,
              explore_cfg={"max_schedules": 4, "max_preemptions": 1}, minimize_cfg={"max_attempts": 6, "max_failures": 1})
    first = evaluate(st, cid, **kw)
    res = first["result"]
    assert first["status"] == "completed", res.get("steps")
    assert res["candidate"]["source_hash"] == register(BAD)["source_hash"]
    assert res["steps"]["benchmark"]["comparisons"][0]["classification"] == "confirmed_violation"
    assert res["steps"]["exploration"]["status"] == "stopped" and not res["steps"]["exploration"]["exhaustive_within_bounds"]
    assert res["steps"]["minimization"]["minimizations"][0]["original_unchanged"]
    rep = res["steps"]["report"]
    assert rep["manifest_verification"]["ok"] and rep["completeness"]["status"] == "complete"
    md = res["summary_markdown"]
    assert register(BAD)["source_hash"] in md and "NOT a security boundary" in md and "not exhaustive within the bounds" in md
    assert "Producer (as claimed in the manifest, not verified)" in md
    stored_first = json.dumps(st.get_analysis(first["id"])["result"], sort_keys=True)
    second = evaluate(st, cid, **{**kw, "explore_cfg": False, "minimize_cfg": False})
    assert second["id"] != first["id"]
    assert second["result"]["steps"]["report"]["experiment_id"] != rep["experiment_id"]
    assert json.dumps(st.get_analysis(first["id"])["result"], sort_keys=True) == stored_first, "first evaluation unchanged"
    assert hashlib.sha256(frozen.read_bytes()).hexdigest() == frozen_sha


def test_cancelled_and_blocked_evaluations_are_distinguishable(env):
    st = env["store"]
    cid = register(GOOD)["candidate_id"]
    c = evaluate(st, cid, scenarios=[S02], seeds=[1], references=["ref_a_trigger"], isolation=ISO, cancel=lambda: True)
    assert c["status"] == "interrupted" and c["result"]["steps"]["benchmark"]["completed"] == 0
    b = evaluate(st, cid, scenarios=[S02], seeds=[1], references=["ref_a_trigger"], isolation={"mode": "strict"})
    assert st.get_analysis(b["id"])["status"] == "blocked" and "strict isolation unavailable" in b["result"]["blocked"]


# -- readiness-audit regressions ---------------------------------------------------------------------
def test_candidates_are_refused_without_the_trusted_environment_declaration(env):
    r = execute_run(F01, register(GOOD)["impl_id"], 1)
    assert r["error_class"] == "isolation_unavailable" and "not a security boundary" in r["infra_error"]
    assert r["database"] is None and r["events"] == [], "nothing may execute"


def test_requested_isolation_limits_reach_every_candidate_run_and_replay(env):
    from gauntlet.execution import replay
    st = env["store"]
    iso = {"trusted_environment": True, "memory_mb": 300, "cpu_seconds": 77}
    out = evaluate(st, register(GOOD)["candidate_id"], scenarios=[S02], seeds=[1], references=["ref_a_trigger"],
                   isolation=iso, explore_cfg={"max_schedules": 2, "max_preemptions": 1}, minimize_cfg=False)
    origins = {out["result"]["steps"]["benchmark"]["comparisons"][0]["id"], out["result"]["steps"]["exploration"]["id"]}
    cand_runs = [st.get_run(r["id"]) for r in st.list_runs(limit=1000)
                 if r["impl_id"].startswith("ext:") and (r.get("origin") or {}).get("id") in origins]
    assert len(cand_runs) >= 3
    for r in cand_runs:
        assert (r["isolation"]["config"]["memory_mb"], r["isolation"]["config"]["cpu_seconds"]) == (300, 77)
    again = replay(st, cand_runs[0])
    assert again["run"]["isolation"]["config"]["memory_mb"] == 300 and again["reproduced"]
    res = out["result"]
    assert res["harness"]["oracle_fingerprint"] and res["harness"]["packages"]["psycopg"]
    assert res["agent_task_bundle"]["files"]["docs/agent_task/TASK.md"]
    assert "Not recorded by GAUNTLET: the prompt actually given to an agent" in res["summary_markdown"]


def test_a_candidate_role_cannot_connect_to_another_run_database(env):
    from psycopg import conninfo
    from gauntlet.pg.provision import create_candidate_role, create_run_database, drop_candidate_role, drop_run_database
    ref_db, cand_db = create_run_database(), create_run_database()
    role, role_db = create_candidate_role(cand_db)
    try:
        p = conninfo.conninfo_to_dict(role_db.dsn)
        p["dbname"] = ref_db.name
        with pytest.raises(psycopg.OperationalError, match="permission denied"):
            psycopg.connect(conninfo.make_conninfo(**p)).close()
        psycopg.connect(role_db.dsn).close()  # its own database still works
    finally:
        drop_run_database(cand_db)
        drop_candidate_role(role)
        drop_run_database(ref_db)


def test_an_evaluation_whose_requested_step_failed_is_not_completed(env, monkeypatch):
    import gauntlet.candidates.evaluate as ev
    real = ev.explore

    def broken(*a, **k):
        out = real(*a, **{**k, "limits": {**k["limits"], "max_schedules": 1}})
        out["result"]["status"], out["result"]["stop_reason"] = "error", "RuntimeError: simulated failure"
        return out
    monkeypatch.setattr(ev, "explore", broken)
    out = evaluate(env["store"], register(GOOD)["candidate_id"], scenarios=[S02], seeds=[1], references=["ref_a_trigger"],
                   isolation=ISO, minimize_cfg=False)
    assert out["status"] == "incomplete" and out["result"]["step_failures"] == ["exploration: error"]
    assert "Steps that did not finish" in out["result"]["summary_markdown"]
