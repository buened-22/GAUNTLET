"""Run time limits against a worker that ignores the controller (PostgreSQL integration).

The test-only implementation (tests/hang_inject/) sleeps for five minutes inside expand. Without the
watchdog the controller would wait for the 20 s step timeout; with a 3 s run limit the watchdog must end
the run promptly as a timeout, leave nothing behind, and not disturb a concurrent run.
"""

import os
import pathlib
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor

import psycopg
import pytest

from gauntlet.config import PG_ADMIN_DSN
from gauntlet.engine.runner import execute_run

pytestmark = pytest.mark.pg

INJECT = pathlib.Path(__file__).resolve().parent / "hang_inject"
sys.path.insert(0, str(INJECT))
import hang_test_impls  # noqa: E402

hang_test_impls.register()
HANG = hang_test_impls.TEST_IMPL_ID
F01 = "F01_backfill_race_probe"


@pytest.fixture
def inject_env(monkeypatch, pg_available):
    monkeypatch.setenv("PYTHONPATH", str(INJECT) + os.pathsep + os.environ.get("PYTHONPATH", ""))


def run_databases() -> list[str]:
    with psycopg.connect(PG_ADMIN_DSN, autocommit=True) as c:
        return [r[0] for r in c.execute("SELECT datname FROM pg_database WHERE datname LIKE 'gauntlet_run_%'")]


def test_hanging_worker_is_stopped_by_the_watchdog_without_leaks(inject_env):
    threads_before = threading.active_count()
    dbs_before = set(run_databases())
    for _ in range(3):  # repeated runs: nothing may accumulate
        t = time.monotonic()
        r = execute_run(F01, HANG, 1, time_limit_s=3)
        took = time.monotonic() - t
        assert r["verdict"] == "infra_error" and r["error_class"] == "timeout", (r["error_class"], r["infra_error"])
        assert all(i["status"] == "infra_error" for i in r["invariants"]), "a timeout is never judged"
        # well under the 20 s step timeout that would end the run without the watchdog
        assert took < 18, f"timed-out run took {took:.1f}s"
    time.sleep(0.5)
    assert threading.active_count() <= threads_before, "watchdog / reader threads leaked"
    assert set(run_databases()) <= dbs_before, "run databases left behind"


def test_timeout_does_not_disturb_a_concurrent_run(inject_env, run_cache):
    alone = run_cache(F01, "ref_a_trigger", 1)
    with ThreadPoolExecutor(max_workers=2) as ex:
        hang = ex.submit(execute_run, F01, HANG, 1, time_limit_s=3)
        normal = ex.submit(execute_run, F01, "ref_a_trigger", 1)
        h, n = hang.result(timeout=300), normal.result(timeout=300)
    assert h["error_class"] == "timeout"
    assert n["infra_error"] is None and n["verdict"] == "pass"
    assert (n["trace_hash"], n["outcome_hash"]) == (alone["trace_hash"], alone["outcome_hash"])


def test_normal_completion_is_unaffected_by_a_generous_limit(pg_available, run_cache):
    alone = run_cache(F01, "ref_a_trigger", 1)
    r = execute_run(F01, "ref_a_trigger", 1, time_limit_s=600)
    assert r["error_class"] is None and r["verdict"] == "pass"
    assert (r["trace_hash"], r["outcome_hash"]) == (alone["trace_hash"], alone["outcome_hash"])
