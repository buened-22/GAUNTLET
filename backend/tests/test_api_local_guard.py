"""The local API refuses requests that a web page could relay through the operator's browser.

The API is unauthenticated and accepts the operator's trust declaration for process isolation
(``isolation.trusted_environment``) in a request body. A page on another site must therefore not be able to
reach it: not through DNS rebinding (the browser sends the attacker's host name in ``Host``), and not through
a cross-site request (the browser sends the attacker's ``Origin``). No database is needed: a refused request
never reaches a route, and the accepted control requests end in a 404 for an unknown candidate.
"""

from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor

import pytest
from fastapi.testclient import TestClient

from gauntlet.api.app import create_app
from gauntlet.api.local_guard import allowed_hosts, allowed_origins, host_name, is_json
from gauntlet.store import Store

EVALUATION = json.dumps({"candidate_id": "cand-0000000000000000",
                         "isolation": {"mode": "process", "trusted_environment": True}})
JSON = {"content-type": "application/json"}


@pytest.fixture()
def store(tmp_path):
    return Store(tmp_path / "results.sqlite3")


@pytest.fixture()
def client(store):
    app = create_app(store=store, executor=ThreadPoolExecutor(max_workers=1), jobs=ThreadPoolExecutor(max_workers=1))
    with TestClient(app, base_url="http://127.0.0.1:8000") as c:
        yield c


def _analyses(store) -> list:
    return store.list_analyses()


def test_local_requests_are_accepted(client):
    """Positive control: the same request, addressed to the loopback interface, reaches the route."""
    assert client.get("/api/meta").status_code == 200
    r = client.post("/api/evaluations", content=EVALUATION, headers=JSON)
    assert r.status_code == 404 and "unknown candidate" in r.text


@pytest.mark.parametrize("host", ["localhost:8000", "127.0.0.1", "LOCALHOST:8000", "[::1]:8000"])
def test_every_loopback_name_is_accepted(client, host):
    assert client.get("/api/meta", headers={"host": host}).status_code == 200


@pytest.mark.parametrize("host", ["evil.example:8000", "evil.example", "127.0.0.1.evil.example:8000",
                                  "localhost.evil.example", "192.168.1.20:8000", ""])
def test_dns_rebinding_host_is_refused_for_reads_and_writes(client, store, host):
    r = client.get("/api/candidates", headers={"host": host})
    assert r.status_code == 403 and "Host header" in r.json()["detail"]
    r = client.post("/api/evaluations", content=EVALUATION, headers={**JSON, "host": host})
    assert r.status_code == 403
    assert _analyses(store) == []  # nothing was queued


@pytest.mark.parametrize("origin", ["https://evil.example", "http://evil.example:8000", "null",
                                    "http://localhost:9999", "http://127.0.0.1.evil.example:8000"])
def test_cross_site_state_changing_request_is_refused(client, store, origin):
    r = client.post("/api/evaluations", content=EVALUATION, headers={**JSON, "origin": origin})
    assert r.status_code == 403 and "web origin" in r.json()["detail"]
    # requests without a body (replay, cancel, report) are refused the same way
    assert client.post("/api/runs/0000000000000000/replay", headers={"origin": origin}).status_code == 403
    assert client.put("/api/experiments/expe-000000000000", content="{}", headers={**JSON, "origin": origin}).status_code == 403
    assert _analyses(store) == []


@pytest.mark.parametrize("origin", ["http://localhost:5173", "http://127.0.0.1:5173", "http://127.0.0.1:8000"])
def test_dashboard_and_same_server_origins_are_accepted(client, origin):
    r = client.post("/api/evaluations", content=EVALUATION, headers={**JSON, "origin": origin})
    assert r.status_code == 404 and "unknown candidate" in r.text


@pytest.mark.parametrize("content_type", ["text/plain", "application/x-www-form-urlencoded", "multipart/form-data",
                                          None])
def test_a_body_that_is_not_json_is_refused(client, store, content_type):
    """The three media types a cross-site form can send without a CORS preflight, and no media type at all
    (which older FastAPI releases parsed as JSON)."""
    req = client.build_request("POST", "/api/evaluations", content=EVALUATION)
    if content_type is None:
        req.headers.pop("content-type", None)
    else:
        req.headers["content-type"] = content_type
    assert req.headers.get("content-type") == content_type
    r = client.send(req)
    assert r.status_code == 415 and "application/json" in r.json()["detail"]
    assert _analyses(store) == []


def test_cors_preflight_from_a_foreign_origin_is_not_approved(client):
    r = client.options("/api/evaluations", headers={"origin": "https://evil.example",
                                                    "access-control-request-method": "POST",
                                                    "access-control-request-headers": "content-type"})
    assert "access-control-allow-origin" not in r.headers
    r = client.options("/api/evaluations", headers={"origin": "http://localhost:5173",
                                                    "access-control-request-method": "POST",
                                                    "access-control-request-headers": "content-type"})
    assert r.headers.get("access-control-allow-origin") == "http://localhost:5173"


def test_additional_hosts_and_origins_are_opt_in(store, monkeypatch):
    monkeypatch.setenv("GAUNTLET_API_ALLOWED_HOSTS", "gauntlet.internal, Lab-Box")
    monkeypatch.setenv("GAUNTLET_API_ALLOWED_ORIGINS", "http://gauntlet.internal:5173/")
    assert {"gauntlet.internal", "lab-box", "127.0.0.1"} <= set(allowed_hosts())
    assert "http://gauntlet.internal:5173" in allowed_origins()
    app = create_app(store=store, executor=ThreadPoolExecutor(max_workers=1), jobs=ThreadPoolExecutor(max_workers=1))
    with TestClient(app, base_url="http://127.0.0.1:8000") as c:
        assert c.get("/api/meta", headers={"host": "gauntlet.internal:8000"}).status_code == 200
        assert c.get("/api/meta", headers={"host": "evil.example"}).status_code == 403
        r = c.post("/api/evaluations", content=EVALUATION, headers={**JSON, "origin": "http://gauntlet.internal:5173"})
        assert r.status_code == 404


def test_header_parsing():
    assert host_name("127.0.0.1:8000") == "127.0.0.1" and host_name("LocalHost") == "localhost"
    assert host_name("[::1]:8000") == "::1" and host_name("[::1]") == "::1"
    assert host_name("evil.example:80") == "evil.example"
    assert is_json("application/json") and is_json("Application/JSON; charset=utf-8")
    assert is_json("application/problem+json")
    assert not is_json("text/plain") and not is_json("") and not is_json("application/jsonx")
