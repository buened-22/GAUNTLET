"""API behaviour, run persistence and report generation."""

import time
from concurrent.futures import ThreadPoolExecutor

import pytest
from fastapi.testclient import TestClient

from gauntlet.api.app import create_app
from gauntlet.bench import summarize
from gauntlet.report import benchmark_report, run_report
from gauntlet.store import Store


@pytest.fixture
def store(tmp_path):
    return Store(tmp_path / "results.sqlite3")


@pytest.fixture
def client(store):
    app = create_app(store=store, executor=ThreadPoolExecutor(max_workers=1))
    with TestClient(app, base_url="http://127.0.0.1:8000") as c:
        yield c


def test_catalogue_endpoints(client):
    meta = client.get("/api/meta").json()
    assert meta["counts"]["scenarios"] == 6 and meta["counts"]["mutants"] >= 2
    assert meta["latest_benchmark"] is None  # nothing executed yet: reported as unavailable, not zero
    assert len(client.get("/api/scenarios").json()) == 6
    assert any(i["kind"] == "mutant" for i in client.get("/api/implementations").json())
    assert len(client.get("/api/invariants").json()) == 12
    assert len(client.get("/api/faults").json()) == 8
    sc = client.get("/api/scenarios/S02_backfill_vs_legacy_writes?seed=4").json()
    assert sc["schedule_preview"]["directives"]
    assert client.get("/api/scenarios/nope").status_code == 404


def test_input_validation(client):
    assert client.post("/api/runs", json={"scenario_id": "nope", "impl_id": "ref_a_trigger", "seed": 1}).status_code == 422
    assert client.post("/api/runs", json={"scenario_id": "S01_baseline_migration", "impl_id": "x", "seed": 1}).status_code == 422
    assert client.post("/api/runs", json={"scenario_id": "S01_baseline_migration", "impl_id": "ref_a_trigger",
                                          "seed": -1}).status_code == 422
    assert client.get("/api/runs/doesnotexist").status_code == 404
    assert client.get("/api/benchmarks/latest").status_code == 404


def test_incomplete_run_is_not_served_as_result(client, store):
    store.create_pending("pending1", "2026-01-01T00:00:00", "0.1.0", "S01_baseline_migration", "ref_a_trigger",
                         "reference", 1)
    assert client.get("/api/runs/pending1").json()["status"] == "queued"
    assert client.get("/api/runs/pending1/invariants").status_code == 409
    assert client.get("/api/runs/pending1/report").status_code == 409


def test_stale_running_rows_marked_on_startup(store):
    store.create_pending("stale1", "2026-01-01T00:00:00", "0.1.0", "S01_baseline_migration", "ref_a_trigger",
                         "reference", 1)
    store.mark_running("stale1")
    create_app(store=store, executor=ThreadPoolExecutor(max_workers=1))
    r = store.get_run("stale1")
    assert r["verdict"] == "infra_error" and "server stopped" in r["infra_error"]


@pytest.mark.pg
def test_run_lifecycle_through_api(client, pg_available):
    r1 = client.post("/api/runs", json={"scenario_id": "S01_baseline_migration", "impl_id": "ref_a_trigger", "seed": 3})
    r2 = client.post("/api/runs", json={"scenario_id": "S01_baseline_migration", "impl_id": "m8_skip_reconcile", "seed": 3})
    assert r1.status_code == 202 and r1.json()["status"] == "queued"
    ids = [r1.json()["id"], r2.json()["id"]]
    deadline = time.time() + 180
    while time.time() < deadline:
        states = [client.get(f"/api/runs/{i}").json()["status"] for i in ids]
        if all(s == "completed" for s in states):
            break
        time.sleep(0.5)
    a, b = (client.get(f"/api/runs/{i}").json() for i in ids)
    assert a["verdict"] == "pass" and b["verdict"] == "fail"
    trace = client.get(f"/api/runs/{ids[0]}/trace").json()
    assert trace["events"] and trace["attempts"] and trace["snapshots"]
    invs = client.get(f"/api/runs/{ids[1]}/invariants").json()
    assert any(i["id"] == "S1_ACKED_DURABLE" and i["status"] == "fail" for i in invs)
    report = client.get(f"/api/runs/{ids[1]}/report").text
    assert "S1_ACKED_DURABLE" in report and "FAIL" in report and "Limitations" in report
    cmp = client.get(f"/api/compare?a={ids[0]}&b={ids[1]}").json()
    assert cmp["same_schedule"] is True and cmp["same_trace"] is False
    assert any(i["differs"] for i in cmp["invariants"])
    assert len(client.get("/api/runs").json()) == 2


def _fake_run(rid, impl, kind, sc, seed, verdict, statuses):
    return {"id": rid, "impl_id": impl, "impl_kind": kind, "scenario_id": sc, "seed": seed, "verdict": verdict,
            "schedule_hash": "s", "trace_hash": "t", "outcome_hash": "o", "statuses": statuses, "infra_error": None,
            "duration_ms": 1.0}


def test_benchmark_summary_and_report_use_only_given_data():
    runs = [
        _fake_run("r1", "ref_a_trigger", "reference", "S02_backfill_vs_legacy_writes", 1, "pass", {"S1_ACKED_DURABLE": "pass"}),
        _fake_run("r2", "m1_stale_backfill", "mutant", "S02_backfill_vs_legacy_writes", 1, "fail", {"S1_ACKED_DURABLE": "fail"}),
        _fake_run("r3", "m4_premature_contract", "mutant", "S05_premature_finalization", 1, "inconclusive",
                  {"C1_COMPATIBILITY": "inconclusive"}),
    ]
    s = summarize(runs, [], {}, ["ref_a_trigger", "m1_stale_backfill", "m4_premature_contract"],
                  ["S02_backfill_vs_legacy_writes", "S05_premature_finalization"], [1])
    assert s["runs_completed"] == 3 and s["invariant_violations"] == 1
    assert s["mutants_detected"] == 1 and s["mutants_killed_as_intended"] == 1
    assert s["inconclusive_runs"] == 1
    m4 = next(m for m in s["mutants"] if m["impl_id"] == "m4_premature_contract")
    assert m4["detected"] is False  # inconclusive is never counted as a detection
    md = benchmark_report({**s, "benchmark_id": "b1", "environment": {"postgres": "16.14"}})
    assert "| Runs completed (matrix) | 3 |" in md and "m1_stale_backfill" in md


@pytest.mark.pg
def test_run_persistence_roundtrip_and_report(store, run_cache):
    run = run_cache("S01_baseline_migration", "ref_b_dualwrite")
    store.save_run(run)
    back = store.get_run(run["id"])
    assert back["verdict"] == run["verdict"] and back["trace_hash"] == run["trace_hash"]
    assert len(back["events"]) == len(run["events"])
    listed = store.list_runs()
    assert listed[0]["id"] == run["id"] and listed[0]["metrics"]["statements"] == run["metrics"]["statements"]
    md = run_report(back)
    assert run["schedule_hash"] in md and "python -m gauntlet run" in md
