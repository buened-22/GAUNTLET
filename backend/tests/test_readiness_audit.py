"""Independent re-audit regressions: safety gate, enforced isolation, database privileges, status paths,
report honesty and provenance. Candidates are SYNTHETIC fixtures (known, safe code), never agent output.

Gate tests use two independent instruments: an import canary (a fixture module that appends to a file named
by GAUNTLET_TEST_IMPORT_CANARY when imported, which proves candidate code never ran on unsandboxed paths) and
an in-process counter on ActorSandbox creation (which proves no sandboxed candidate actor was even started).
"""

import json
import os
import shutil
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import psycopg
import pytest
from fastapi.testclient import TestClient

from gauntlet.candidates.snapshots import register
from gauntlet.config import PG_ADMIN_DSN
from gauntlet.engine.runner import execute_run
from gauntlet.store import Store

pytestmark = pytest.mark.pg
HERE = Path(__file__).resolve().parent
CANARY = HERE / "candidates" / "synthetic_import_canary"
GOOD = HERE / "candidates" / "synthetic_ref_a_copy"
BAD = HERE / "candidates" / "synthetic_stale_backfill"
F01, S02 = "F01_backfill_race_probe", "S02_backfill_vs_legacy_writes"
TRUSTED = {"trusted_environment": True}


@pytest.fixture(scope="module")
def env(tmp_path_factory, pg_available):
    root = tmp_path_factory.mktemp("audit")
    old = {k: os.environ.get(k) for k in ("GAUNTLET_CANDIDATES_DIR", "GAUNTLET_TEST_IMPORT_CANARY")}
    os.environ["GAUNTLET_CANDIDATES_DIR"] = str(root / "snapshots")
    marker = root / "canary.log"
    os.environ["GAUNTLET_TEST_IMPORT_CANARY"] = str(marker)
    yield {"root": root, "store": Store(root / "results.sqlite3"), "marker": marker}
    for k, v in old.items():
        if v is None:
            os.environ.pop(k, None)
        else:
            os.environ[k] = v
    with psycopg.connect(PG_ADMIN_DSN, autocommit=True) as c:
        dbs = [r[0] for r in c.execute("SELECT datname FROM pg_database WHERE datname LIKE 'gauntlet_run_%'")]
        roles = [r[0] for r in c.execute("SELECT rolname FROM pg_roles WHERE rolname LIKE 'gauntlet_cand_%'")]
    assert dbs == [] and roles == [], (dbs, roles)


@pytest.fixture
def sandbox_counter(monkeypatch):
    import gauntlet.candidates.sandbox as sb
    created = []
    real = sb.ActorSandbox.__init__

    def counting(self, *a, **k):
        created.append(1)
        real(self, *a, **k)
    monkeypatch.setattr(sb.ActorSandbox, "__init__", counting)
    return created


def canary_fired(env) -> bool:
    return env["marker"].exists() and env["marker"].read_text(encoding="utf-8").strip() != ""


# -- 1. the gate: no entry point executes a candidate without the operator declaration ---------------
def test_canary_positive_control(env, tmp_path):
    marker = tmp_path / "control.log"
    code = f"import sys; sys.path.insert(0, r'{CANARY}'); import candidate"
    r = subprocess.run([sys.executable, "-c", code], env={**os.environ, "GAUNTLET_TEST_IMPORT_CANARY": str(marker)},
                       cwd=str(HERE.parent), capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    assert marker.read_text(encoding="utf-8").strip() == "candidate module imported"


def test_direct_run_without_declaration_never_starts_a_candidate(env, sandbox_counter):
    impl = register(CANARY)["impl_id"]
    r = execute_run(F01, impl, 1)
    assert r["error_class"] == "isolation_unavailable" and r["verdict"] == "infra_error"
    assert sandbox_counter == [] and not canary_fired(env) and r["database"] is None


def test_benchmark_driver_cannot_bypass_the_gate(env, sandbox_counter):
    from gauntlet.bench import run_benchmark
    impl = register(CANARY)["impl_id"]
    s = run_benchmark([impl], [F01], [1], reproducibility=False, conventional=False, store=env["store"],
                      progress=lambda *_: None)
    assert s["verdicts"]["infra_error"] == 1 and s["verdicts"]["pass"] == 0
    assert sandbox_counter == [] and not canary_fired(env)


def test_conventional_suite_never_imports_a_candidate(env):
    from gauntlet.conventional import run_suite
    impl = register(CANARY)["impl_id"]
    res = run_suite(impl)
    assert res["counts"]["pass"] == 0 and not res["all_passed"]
    assert not canary_fired(env), "the unsandboxed conventional path must refuse to load candidate code"


def test_replay_of_a_blocked_run_stays_blocked(env, sandbox_counter):
    from gauntlet.execution import replay
    impl = register(CANARY)["impl_id"]
    blocked = execute_run(F01, impl, 1)
    again = replay(env["store"], blocked)["run"]
    assert again["error_class"] == "isolation_unavailable" and sandbox_counter == [] and not canary_fired(env)


def test_api_entry_points_cannot_bypass_the_gate(env, sandbox_counter):
    from gauntlet.api.app import create_app
    impl = register(CANARY)["impl_id"]
    cid = impl.split(":", 1)[1]
    st = env["store"]
    with TestClient(create_app(store=st, executor=ThreadPoolExecutor(max_workers=1),
                               jobs=ThreadPoolExecutor(max_workers=1)), base_url="http://127.0.0.1:8000") as c:
        assert c.post("/api/runs", json={"scenario_id": F01, "impl_id": impl, "seed": 1}).status_code == 422
        ids = [c.post("/api/differential", json={"impl_ids": ["ref_a_trigger", impl], "scenario_id": F01}).json()["id"],
               c.post("/api/explorations", json={"scenario_id": F01, "impl_id": impl, "limits": {"max_schedules": 2}}).json()["id"],
               c.post("/api/evaluations", json={"candidate_id": cid, "scenarios": [F01], "explore": None,
                                                "minimize_max_attempts": None}).json()["id"]]
        deadline = time.time() + 300
        while time.time() < deadline and any(st.get_analysis(i)["status"] in ("queued", "running") for i in ids):
            time.sleep(1)
        d, x, e = (st.get_analysis(i) for i in ids)
    cand = next(r for r in d["result"]["runs"] if r["impl_id"] == impl)
    assert cand["error_class"] == "isolation_unavailable" and d["result"]["classification"] == "inconclusive"
    assert x["result"]["outcomes"] == {"isolation_unavailable": 2} or set(x["result"]["outcomes"]) == {"isolation_unavailable"}
    assert x["status"] != "completed" or not x["result"]["exhaustive_within_bounds"]
    assert e["status"] == "blocked" and "summary_markdown" not in (e["result"] or {})
    assert sandbox_counter == [] and not canary_fired(env)


# -- 2. requested limits are actually enforced on every path -----------------------------------------
def _probe_candidate(env) -> str:
    d = env["root"] / "memory_probe"
    if not d.exists():
        shutil.copytree(BAD, d)
        src = (d / "candidate.py").read_text(encoding="utf-8")
        src += ("\n\nclass Probe(Implementation):\n"
                "    def migrator_expand(self, db, a):\n"
                "        try:\n            block = bytearray(300 * 1024 * 1024)\n            probe = 'allocated'\n            del block\n"
                "        except MemoryError:\n            probe = 'memoryerror'\n"
                "        res = super().migrator_expand(db, a)\n        res['memory_probe'] = probe\n        return res\n")
        (d / "candidate.py").write_text(src, encoding="utf-8")
        m = json.loads((d / "gauntlet_candidate.json").read_text(encoding="utf-8"))
        m["name"], m["entry"]["class"] = "synthetic-memory-probe", "Probe"
        (d / "gauntlet_candidate.json").write_text(json.dumps(m), encoding="utf-8")
    return register(d)["candidate_id"]


def _probes(store, run_ids):
    out = {}
    for rid in run_ids:
        r = store.get_run(rid)
        exp = [a for a in r["attempts"] if a["op"] == "expand" and a["status"] == "ok"]
        if exp:
            out[rid] = exp[0]["result"]["memory_probe"]
    return out


@pytest.mark.skipif(not sys.platform.startswith("win"), reason="memory enforcement verified only for the Windows job object")
def test_memory_limit_is_enforced_in_evaluation_exploration_replay_and_minimization(env):
    from gauntlet.candidates.evaluate import evaluate
    from gauntlet.execution import replay
    st = env["store"]
    cid = _probe_candidate(env)
    out = evaluate(st, cid, scenarios=[S02], seeds=[1], references=["ref_a_trigger"],
                   isolation={**TRUSTED, "memory_mb": 150},
                   explore_cfg={"max_schedules": 2, "max_preemptions": 1}, minimize_cfg={"max_attempts": 3, "max_failures": 1})
    res = out["result"]
    impl = f"ext:{cid}"
    all_cand = [r["id"] for r in st.list_runs(limit=5000) if r["impl_id"] == impl]
    probes = _probes(st, all_cand)
    kinds = {(st.get_run(r).get("origin") or {}).get("kind") for r in probes}
    assert {"differential", "exploration", "minimization"} <= kinds, kinds
    assert set(probes.values()) == {"memoryerror"}, probes
    bench_run = res["steps"]["benchmark"]["comparisons"][0]["candidate_run"]
    rep = replay(st, st.get_run(bench_run))
    assert _probes(st, [rep["replay_id"]]) == {rep["replay_id"]: "memoryerror"}
    assert st.get_run(bench_run)["isolation"]["enforcement"]["memory_mb"].startswith("enforced")
    # control: the default limit (512 MB) lets the same allocation succeed
    ctl = execute_run(S02, impl, 1, isolation=TRUSTED)
    exp = next(a for a in ctl["attempts"] if a["op"] == "expand")
    assert exp["result"]["memory_probe"] == "allocated"


# -- 3. database privileges ---------------------------------------------------------------------------
def test_cross_run_connection_is_refused_and_the_test_discriminates(env):
    from psycopg import conninfo
    from gauntlet.pg.provision import create_candidate_role, create_run_database, drop_candidate_role, drop_run_database, RunDatabase
    cand_db = create_run_database()
    role, role_db = create_candidate_role(cand_db)
    raw_name = "gauntlet_run_auditctl" + os.urandom(4).hex()
    with psycopg.connect(PG_ADMIN_DSN, autocommit=True) as c:  # positive control: a database WITHOUT the revoke
        c.execute(f'CREATE DATABASE "{raw_name}" TEMPLATE template0')
    ref_db = create_run_database()
    try:
        p = conninfo.conninfo_to_dict(role_db.dsn)
        p["dbname"] = raw_name
        psycopg.connect(conninfo.make_conninfo(**p)).close()  # the defect: connects when PUBLIC keeps CONNECT
        p["dbname"] = ref_db.name
        with pytest.raises(psycopg.OperationalError, match="permission denied"):
            psycopg.connect(conninfo.make_conninfo(**p)).close()
        p["dbname"] = "postgres"
        with psycopg.connect(conninfo.make_conninfo(**p), autocommit=True) as c:
            for sql in ("CREATE TABLE public.audit_probe(i int)", "CREATE DATABASE audit_probe_db", "CREATE ROLE audit_probe_role",
                        "ALTER ROLE CURRENT_USER CREATEDB"):
                with pytest.raises(psycopg.errors.InsufficientPrivilege):
                    c.execute(sql)
    finally:
        drop_run_database(cand_db)
        drop_candidate_role(role)
        drop_run_database(ref_db)
        drop_run_database(RunDatabase(name=raw_name, dsn=""))


# -- 4. status paths ------------------------------------------------------------------------------------
def test_timed_out_candidate_makes_the_evaluation_incomplete(env):
    from gauntlet.candidates.evaluate import evaluate
    d = env["root"] / "spinner"
    if not d.exists():
        shutil.copytree(GOOD, d)
        src = (d / "candidate.py").read_text(encoding="utf-8")
        src += "\n\nclass Spin(Implementation):\n    def migrator_expand(self, db, a):\n        while True:\n            pass\n"
        (d / "candidate.py").write_text(src, encoding="utf-8")
        m = json.loads((d / "gauntlet_candidate.json").read_text(encoding="utf-8"))
        m["name"], m["entry"]["class"] = "synthetic-spinner", "Spin"
        (d / "gauntlet_candidate.json").write_text(json.dumps(m), encoding="utf-8")
    cid = register(d)["candidate_id"]
    out = evaluate(env["store"], cid, scenarios=[F01], seeds=[1], references=["ref_a_trigger"], isolation=TRUSTED,
                   run_time_limit_s=8, explore_cfg=False, minimize_cfg=False)
    assert out["status"] == "incomplete"
    assert out["result"]["steps"]["benchmark"]["comparisons"][0]["candidate_outcome"] == "timeout"
    assert "Status: **incomplete**" in out["result"]["summary_markdown"]


def test_internal_error_is_reported_as_error_never_completed(env, monkeypatch):
    import gauntlet.candidates.evaluate as ev
    from gauntlet.api.app import create_app
    cid = register(GOOD)["candidate_id"]

    def boom(*a, **k):
        raise RuntimeError("simulated harness failure")
    monkeypatch.setattr(ev, "run_differential", boom)
    st = env["store"]
    with TestClient(create_app(store=st, executor=ThreadPoolExecutor(max_workers=1),
                               jobs=ThreadPoolExecutor(max_workers=1)), base_url="http://127.0.0.1:8000") as c:
        eid = c.post("/api/evaluations", json={"candidate_id": cid, "scenarios": [F01], "isolation": TRUSTED,
                                               "explore": None, "minimize_max_attempts": None}).json()["id"]
        deadline = time.time() + 60
        while time.time() < deadline and st.get_analysis(eid)["status"] in ("queued", "running"):
            time.sleep(0.5)
        a = c.get(f"/api/evaluations/{eid}").json()
        assert a["status"] == "error" and "simulated harness failure" in a["result"]["stop_reason"]
        assert "summary_markdown" not in a["result"]
        assert c.get(f"/api/evaluations/{eid}/summary.md").status_code == 409


def test_cli_exit_codes_distinguish_blocked_and_completed(env, monkeypatch, capsys):
    import gauntlet.store as store_mod
    from gauntlet import cli
    tmp_store = env["root"] / "cli.sqlite3"
    monkeypatch.setattr(store_mod, "Store", lambda *a, **k: Store(tmp_store))
    cid = register(GOOD)["candidate_id"]
    assert cli.main(["candidate", "evaluate", "--id", cid, "--scenarios", F01, "--refs", "ref_a_trigger",
                     "--no-explore", "--no-minimize"]) == 3
    assert "BLOCKED" in capsys.readouterr().err
    assert cli.main(["candidate", "evaluate", "--id", cid, "--trusted-dev-env", "--scenarios", F01,
                     "--refs", "ref_a_trigger", "--no-explore", "--no-minimize"]) == 0


# -- 5. report honesty and provenance ------------------------------------------------------------------
def test_reports_do_not_present_claimed_or_synthetic_producers_as_evaluated_agents(env):
    from gauntlet.candidates.evaluate import evaluate
    from gauntlet.experiment import generate_report
    d = env["root"] / "claims_agent"
    if not d.exists():
        shutil.copytree(GOOD, d)
        m = json.loads((d / "gauntlet_candidate.json").read_text(encoding="utf-8"))
        m["name"] = "synthetic-claims-agent"
        m["producer"] = {"kind": "ai_agent", "agent": "ExampleAgent", "model": "example-model"}
        (d / "gauntlet_candidate.json").write_text(json.dumps(m), encoding="utf-8")
        (d / "candidate.py").write_text((d / "candidate.py").read_text(encoding="utf-8") + "\n# variant\n", encoding="utf-8")
    st = env["store"]
    for cid in (register(d)["candidate_id"], register(GOOD)["candidate_id"]):
        out = evaluate(st, cid, scenarios=[F01], seeds=[1], references=["ref_a_trigger"], isolation=TRUSTED,
                       explore_cfg=False, minimize_cfg=False)
        md = generate_report(st, out["result"]["steps"]["report"]["experiment_id"], record=False)["markdown"]
        assert "every implementation is a hand-written reference or mutant" not in md
        assert "* Agents:" not in md
        assert "external candidate" in md.lower() and "not verified" in md.lower()


def test_evaluation_provenance_fingerprints_the_evaluated_harness_source(env):
    import hashlib
    from gauntlet.candidates.evaluate import evaluate
    st = env["store"]
    out = evaluate(st, register(GOOD)["candidate_id"], scenarios=[F01], seeds=[1], references=["ref_a_trigger"],
                   isolation=TRUSTED, explore_cfg=False, minimize_cfg=False)
    h = out["result"]["harness"]
    pkg = HERE.parent / "gauntlet"
    files = sorted(p for p in pkg.rglob("*.py") if "__pycache__" not in p.parts)
    doc = {p.relative_to(pkg).as_posix(): hashlib.sha256(p.read_bytes().replace(b"\r\n", b"\n")).hexdigest() for p in files}
    expect = hashlib.sha256(json.dumps(doc, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    assert h["source_fingerprint"] == expect
    assert h["source_changed_since_process_start"] is False
    assert h["postgres"] and h["postgres"].startswith("16")
    run = st.get_run(out["result"]["steps"]["benchmark"]["comparisons"][0]["candidate_run"])
    assert run["provenance"]["harness_source_fingerprint"] == expect
