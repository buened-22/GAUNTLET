"""Conventional functional test suite.

What a careful engineer would typically write for this feature: sequential,
single-client tests of the stated functional requirements, with assertions
made on API responses (plus a schema check after the migration). No
concurrency, no faults, no oracle. The suite exists so that adversarial
results can be compared with ordinary test results on the same
implementations.

It is run through the same actor processes as the adversarial engine (so
implementation code still never runs in the harness process), but every
operation runs to completion before the next one starts.
"""

from __future__ import annotations

import copy
import traceback
from dataclasses import dataclass
from typing import Callable

import psycopg

from gauntlet.actor.proxy import ProcessPool
from gauntlet.config import PG_ADMIN_DSN
from gauntlet.engine.controller import Controller
from gauntlet.engine.observe import capture
from gauntlet.pg.provision import (
    InfrastructureError, apply_legacy_schema, create_run_database, drop_run_database,
)


class Session:
    def __init__(self, impl_id: str, initial: dict[int, str], admin_dsn: str = PG_ADMIN_DSN):
        self.admin_dsn = admin_dsn
        self.db = create_run_database(admin_dsn, prefix="gauntlet_ct")
        apply_legacy_schema(self.db.dsn, initial)
        self.monitor = psycopg.connect(self.db.dsn, autocommit=True)
        self.ctl = Controller(impl_id, self.db.dsn, self.monitor, {}, ProcessPool(3))
        self._n = 0

    def spawn(self, actor: str, role: str) -> None:
        self.ctl._d_spawn({"actor": actor, "role": role})

    def drain(self, actor: str) -> None:
        self.ctl._d_drain({"actor": actor})

    def call(self, actor: str, op: str, **args) -> dict:
        self._n += 1
        lid = f"T{self._n:03d}"
        if op in ("create_order", "set_total", "add_charge") and "key" not in args:
            args["key"] = f"key-{lid}"
        self.ctl.ops[lid] = {"op": op, "args": copy.deepcopy(args), "role": self.ctl.actors[actor].role}
        self.ctl._invoke(actor, lid)
        if self.ctl._run(actor) != "done":
            raise InfrastructureError(f"{op} blocked in a sequential test")
        return self.ctl.outcomes[self.ctl.attempts[lid][-1]]

    def schema(self) -> dict:
        return capture(self.monitor)

    def close(self) -> None:
        try:
            self.ctl.shutdown()
        finally:
            self.monitor.close()
            drop_run_database(self.db, self.admin_dsn)


class TestFailure(AssertionError):
    pass


def ok(outcome: dict) -> dict:
    if outcome["status"] != "ok":
        raise TestFailure(f"expected success, got {outcome}")
    return outcome["result"]


def err(outcome: dict, code: str | None = None) -> dict:
    if outcome["status"] != "error":
        raise TestFailure(f"expected an error, got {outcome}")
    if code and outcome["error"].get("code") != code:
        raise TestFailure(f"expected error {code}, got {outcome['error']}")
    return outcome["error"]


def eq(actual, expected, what: str) -> None:
    if actual != expected:
        raise TestFailure(f"{what}: expected {expected!r}, got {actual!r}")


@dataclass
class CTest:
    id: str
    title: str
    initial: dict[int, str]
    fn: Callable[[Session], None]


def ct01(s: Session) -> None:
    s.spawn("v1", "v1")
    ok(s.call("v1", "create_order", order_id=100, total="19.99"))
    eq(ok(s.call("v1", "get_order", order_id=100))["total"], "19.99", "v1 read-back")


def ct02(s: Session) -> None:
    s.spawn("v1", "v1")
    eq(ok(s.call("v1", "add_charge", order_id=1, amount="2.50"))["total"], "12.50", "charge result")
    eq(ok(s.call("v1", "get_order", order_id=1))["total"], "12.50", "total after charge")


def ct03(s: Session) -> None:
    s.spawn("v1", "v1")
    first = ok(s.call("v1", "add_charge", order_id=1, amount="2.50", key="dup"))
    second = ok(s.call("v1", "add_charge", order_id=1, amount="2.50", key="dup"))
    eq(second, first, "replayed response")
    eq(ok(s.call("v1", "get_order", order_id=1))["total"], "12.50", "applied once")


def ct04(s: Session) -> None:
    s.spawn("v1", "v1")
    err(s.call("v1", "get_order", order_id=999), "not_found")
    err(s.call("v1", "add_charge", order_id=999, amount="1.00"), "not_found")


def ct05(s: Session) -> None:
    s.spawn("m", "migrator")
    ok(s.call("m", "expand"))
    s.spawn("v2", "v2")
    eq(ok(s.call("v2", "get_order", order_id=1))["total"], "10.00", "v2 reads legacy row")
    eq(ok(s.call("v2", "get_order", order_id=2))["total"], "0.10", "v2 reads legacy row")


def ct06(s: Session) -> None:
    s.spawn("m", "migrator")
    ok(s.call("m", "expand"))
    s.spawn("v2", "v2")
    ok(s.call("v2", "create_order", order_id=200, total="19.99"))
    eq(ok(s.call("v2", "get_order", order_id=200))["total"], "19.99", "v2 create read-back")
    ok(s.call("v2", "set_total", order_id=200, total="5.00"))
    eq(ok(s.call("v2", "add_charge", order_id=200, amount="2.50"))["total"], "7.50", "v2 charge")
    eq(ok(s.call("v2", "get_order", order_id=200))["total"], "7.50", "v2 total")


def ct07(s: Session) -> None:
    s.spawn("v1", "v1")
    s.spawn("m", "migrator")
    ok(s.call("m", "expand"))
    for _ in range(10):
        if ok(s.call("m", "backfill_batch", batch_size=2))["done"]:
            break
    s.drain("v1")
    ok(s.call("m", "finalize"))
    ok(s.call("m", "contract"))
    s.spawn("v2", "v2")
    for oid, total in CT07_INITIAL.items():
        eq(ok(s.call("v2", "get_order", order_id=oid))["total"], total, f"order {oid} after migration")
    sch = s.schema()
    eq("total" in sch["columns"], False, "legacy column dropped")
    eq(sch["not_null"].get("total_cents"), True, "total_cents NOT NULL")


def ct08(s: Session) -> None:
    s.spawn("v1", "v1")
    s.spawn("m", "migrator")
    ok(s.call("m", "expand"))
    while not ok(s.call("m", "backfill_batch", batch_size=10))["done"]:
        pass
    err(s.call("m", "finalize"), "migration_refused")
    eq(ok(s.call("v1", "get_order", order_id=1))["total"], "10.00", "legacy instance keeps working")


def ct09(s: Session) -> None:
    s.spawn("m", "migrator")
    ok(s.call("m", "expand"))
    while not ok(s.call("m", "backfill_batch", batch_size=10))["done"]:
        pass
    ok(s.call("m", "finalize"))
    ok(s.call("m", "contract"))
    s.spawn("v2", "v2")
    eq(ok(s.call("v2", "add_charge", order_id=1, amount="0.25"))["total"], "10.25", "charge after contract")
    ok(s.call("v2", "create_order", order_id=300, total="3.00"))
    eq(ok(s.call("v2", "get_order", order_id=300))["total"], "3.00", "create after contract")


def ct10(s: Session) -> None:
    s.spawn("v1", "v1")
    s.spawn("m", "migrator")
    ok(s.call("m", "expand"))
    s.spawn("v2", "v2")
    err(s.call("v1", "set_total", order_id=1, total="1.234"))
    err(s.call("v2", "set_total", order_id=1, total="abc"))
    eq(ok(s.call("v2", "get_order", order_id=1))["total"], "10.00", "unchanged after rejected writes")


BASIC = {1: "10.00", 2: "0.10"}
CT07_INITIAL = {1: "10.00", 2: "0.10", 3: "123.45", 4: "7.07", 5: "99.99", 6: "1.01"}

TESTS = [
    CTest("CT01", "v1 create then read returns the same total", {}, ct01),
    CTest("CT02", "v1 add_charge returns and persists the new total", BASIC, ct02),
    CTest("CT03", "v1 add_charge with a repeated key is applied once", BASIC, ct03),
    CTest("CT04", "unknown orders are reported as not_found", BASIC, ct04),
    CTest("CT05", "after expand, v2 reads legacy rows correctly", BASIC, ct05),
    CTest("CT06", "v2 create / set_total / add_charge / read", BASIC, ct06),
    CTest("CT07", "full sequential migration preserves every value", CT07_INITIAL, ct07),
    CTest("CT08", "finalize is refused while a v1 instance is registered", BASIC, ct08),
    CTest("CT09", "v2 operates normally after contract", BASIC, ct09),
    CTest("CT10", "invalid amounts are rejected without side effects", BASIC, ct10),
]


def run_suite(impl_id: str, admin_dsn: str = PG_ADMIN_DSN) -> dict:
    results = []
    for t in TESTS:
        s = None
        try:
            s = Session(impl_id, t.initial, admin_dsn)
            t.fn(s)
            results.append({"id": t.id, "title": t.title, "status": "pass", "message": ""})
        except TestFailure as exc:
            results.append({"id": t.id, "title": t.title, "status": "fail", "message": str(exc)[:500]})
        except InfrastructureError as exc:
            results.append({"id": t.id, "title": t.title, "status": "infra_error", "message": str(exc)[:500]})
        except Exception as exc:  # harness defect: never counted as pass
            results.append({"id": t.id, "title": t.title, "status": "infra_error",
                            "message": f"{type(exc).__name__}: {exc} {traceback.format_exc(limit=3)}"[:800]})
        finally:
            if s is not None:
                s.close()
    counts = {k: sum(1 for r in results if r["status"] == k) for k in ("pass", "fail", "infra_error")}
    return {"impl_id": impl_id, "tests": results, "counts": counts,
            "all_passed": counts["pass"] == len(TESTS)}
