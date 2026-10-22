"""API for differential testing, exploration, minimization, replay and experiments."""

import time
from concurrent.futures import ThreadPoolExecutor

import pytest
from fastapi.testclient import TestClient

from gauntlet.api.app import create_app
from gauntlet.store import Store

S02 = "S02_backfill_vs_legacy_writes"
F01 = "F01_backfill_race_probe"


@pytest.fixture
def client(tmp_path):
    st = Store(tmp_path / "api.sqlite3")
    app = create_app(store=st, executor=ThreadPoolExecutor(max_workers=1), jobs=ThreadPoolExecutor(max_workers=1))
    with TestClient(app) as c:
        c.store = st
        yield c


def wait(client, path, timeout=900):
    t = time.time()
    while time.time() - t < timeout:
        a = client.get(path).json()
        if a["status"] not in ("queued", "running"):
            return a
        time.sleep(1)
    raise AssertionError(f"{path} still running")


def test_validation(client):
    assert client.post("/api/differential", json={"impl_ids": ["ref_a_trigger"], "scenario_id": S02}).status_code == 422
    assert client.post("/api/differential", json={"impl_ids": ["ref_a_trigger", "nope"], "scenario_id": S02}).status_code == 422
    assert client.post("/api/differential", json={"impl_ids": ["ref_a_trigger", "ref_b_dualwrite"], "scenario_id": "nope"}).status_code == 422
    assert client.post("/api/explorations", json={"scenario_id": F01, "impl_id": "m1_stale_backfill",
                                                  "limits": {"max_preemptions": 9}}).status_code == 422
    assert client.post("/api/explorations", json={"scenario_id": F01, "impl_id": "nope"}).status_code == 422
    assert client.post("/api/minimizations", json={"run_id": "nope"}).status_code == 404
    assert client.get("/api/differential/nope").status_code == 404
    assert client.post("/api/analyses/nope/cancel").status_code == 404
    assert client.post("/api/experiments", json={"config": {"title": "x", "selection": {}}}).status_code == 422
    assert [f["id"] for f in client.get("/api/fixtures").json()] == [F01]
    rules = client.get("/api/differential/rules").json()
    assert len(rules["normalization_rules"]) == 6 and "agreement" in rules["classifications"]


def test_experiment_flow_over_http(client):
    st = client.store
    for rid, impl, kind, verdict in (("e1", "ref_a_trigger", "reference", "pass"), ("e2", "m1_stale_backfill", "mutant", "fail")):
        st.save_run({"id": rid, "created_at": "2026-01-01T00:00:00", "benchmark_version": "0.3.0", "scenario_id": S02,
                     "impl_id": impl, "impl_kind": kind, "seed": 1, "verdict": verdict, "schedule_hash": "s",
                     "trace_hash": rid, "outcome_hash": rid, "duration_ms": 1.0, "events": [],
                     "invariants": [{"id": "S1_ACKED_DURABLE", "status": "fail" if verdict == "fail" else "pass"}]})
    eid = client.post("/api/experiments", json={"config": {"title": "http", "selection": {"run_ids": ["e1", "e2"]}}}).json()["id"]
    rep = client.post(f"/api/experiments/{eid}/report").json()
    m = {x["id"]: x for x in rep["results"]["metrics"]}
    assert (m["M2"]["numerator"], m["M2"]["denominator"]) == (1, 2)
    md = client.get(f"/api/experiments/{eid}/report.md").text
    assert md == rep["markdown"]
    assert client.get(f"/api/experiments/{eid}/export").json()["format"] == "gauntlet-experiment-bundle/1"
    assert client.put(f"/api/experiments/{eid}", json={"changes": {"title": "renamed"}}).status_code == 422  # reported: reason required
    assert client.put(f"/api/experiments/{eid}", json={"changes": {"title": "renamed"}, "reason": "typo"}).status_code == 200
    assert client.get(f"/api/experiments/{eid}").json()["config"]["versions"][-1]["config"]["title"] == "renamed"
    assert any(a["id"] == eid for a in client.get("/api/analyses?kind=experiment").json())


@pytest.mark.pg
def test_long_jobs_end_to_end(client, pg_available):
    # differential comparison as a background job, then its replay
    cid = client.post("/api/differential", json={"impl_ids": ["ref_a_trigger", "m1_stale_backfill"], "scenario_id": S02,
                                                 "seed": 1}).json()["id"]
    d = wait(client, f"/api/differential/{cid}")
    assert d["status"] == "completed" and d["result"]["classification"] == "confirmed_violation"
    assert d["config"]["schedule"]["directives"], "full configuration persisted"
    m1_run = next(r["id"] for r in d["result"]["runs"] if r["impl_id"] == "m1_stale_backfill")
    rid = client.post(f"/api/differential/{cid}/replay").json()["id"]
    rep = wait(client, f"/api/replays/{rid}")
    assert rep["status"] == "completed" and rep["result"]["reproduced"], rep

    # bounded exploration, schedule export, run replay
    eid = client.post("/api/explorations", json={"scenario_id": F01, "impl_id": "m1_stale_backfill",
                                                 "limits": {"max_schedules": 2}}).json()["id"]
    e = wait(client, f"/api/explorations/{eid}")
    assert e["status"] == "stopped" and e["result"]["schedules_executed"] == 2 and not e["result"]["exhaustive_within_bounds"]
    assert e["result"]["unexplored_known"] > 0
    ex = client.get(f"/api/explorations/{eid}/schedules/1/export").json()
    assert ex["schedule"]["directives"][ex["directive_index"]]["choices"] and ex["trace"]
    rr = client.post(f"/api/runs/{ex['run_id']}/replay").json()["id"]
    rr = wait(client, f"/api/replays/{rr}")
    assert rr["result"]["reproduced"], rr

    # minimization with a tiny budget: must report budget exhaustion, not success
    mid = client.post("/api/minimizations", json={"run_id": m1_run, "invariant": "S1_ACKED_DURABLE",
                                                  "budget": {"max_attempts": 2}}).json()["id"]
    mz = wait(client, f"/api/minimizations/{mid}")
    assert mz["status"] == "budget_exhausted" and not mz["result"].get("one_minimal") and mz["result"]["original_unchanged"]

    # cancellation of a long exploration
    xid = client.post("/api/explorations", json={"scenario_id": F01, "impl_id": "ref_a_trigger",
                                                 "limits": {"max_schedules": 50}}).json()["id"]
    assert client.post(f"/api/analyses/{xid}/cancel").json()["cancel_requested"]
    x = wait(client, f"/api/explorations/{xid}")
    assert x["status"] == "interrupted" and x["result"]["schedules_executed"] <= 1 and not x["result"]["exhaustive_within_bounds"]

    # analyses left 'running' by a dead server are marked interrupted on restart
    sid = client.store.create_analysis("exploration", {"x": 1}, status="running")
    create_app(store=client.store, executor=ThreadPoolExecutor(max_workers=1), jobs=ThreadPoolExecutor(max_workers=1))
    assert client.store.get_analysis(sid)["status"] == "interrupted"
