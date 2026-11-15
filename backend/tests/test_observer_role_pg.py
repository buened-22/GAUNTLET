"""Audit finding A-02: the harness observes an external candidate's run database through a per-run,
non-superuser observer role, and never opens a superuser session in that database.

The candidates here are SYNTHETIC, trusted fixtures written for these tests (never AI-generated code); process
isolation is not a security boundary. Some fixtures deliberately try to make the harness execute candidate code
with elevated privileges; the positive controls show that the same objects do escalate when a superuser reads
them, so the tests can detect an escalation.
"""

from __future__ import annotations

import json
import pathlib
import shutil
import uuid

import psycopg
import pytest
from psycopg import conninfo, sql

from gauntlet.candidates.snapshots import register
from gauntlet.config import PG_ADMIN_DSN
from gauntlet.engine.observe import capture
from gauntlet.engine.runner import execute_run, open_monitor, open_observer
from gauntlet.pg.provision import (
    apply_legacy_schema, create_candidate_role, create_observer_role, create_run_database, drop_candidate_role,
    drop_run_database, grant_observer_read,
)

pytestmark = pytest.mark.pg

HERE = pathlib.Path(__file__).resolve().parent
GOOD = HERE / "candidates" / "synthetic_ref_a_copy"
ISO = {"trusted_environment": True}
S01, S04 = "S01_baseline_migration", "S04_crash_and_resume"
ADMIN_USER = conninfo.conninfo_to_dict(PG_ADMIN_DSN).get("user")


def _leftovers() -> tuple:
    with psycopg.connect(PG_ADMIN_DSN, autocommit=True) as c:
        dbs = sorted(r[0] for r in c.execute("SELECT datname FROM pg_database WHERE datname LIKE 'gauntlet_run_%'"))
        roles = sorted(r[0] for r in c.execute(
            "SELECT rolname FROM pg_roles WHERE rolname LIKE 'gauntlet_cand_%' OR rolname LIKE 'gauntlet_obs_%' "
            "OR rolname LIKE 'gauntlet_pwned_%'"))
    return dbs, roles


@pytest.fixture
def env(tmp_path, monkeypatch, pg_available):
    monkeypatch.setenv("GAUNTLET_CANDIDATES_DIR", str(tmp_path / "snapshots"))
    before = _leftovers()
    yield tmp_path
    assert _leftovers() == before, "a run database, a run role or an escalation marker role was left behind"


@pytest.fixture
def run_roles(pg_available):
    """A provisioned run database as an external run sees it: candidate role (owner, P0 schema) and observer."""
    db = create_run_database()
    cand, cand_db = create_candidate_role(db)
    obs, obs_db = create_observer_role(db)
    try:
        apply_legacy_schema(cand_db.dsn, {1: "10.00", 2: "20.50"})
        grant_observer_read(cand_db.dsn, obs)
        yield {"db": db, "cand": cand, "cand_dsn": cand_db.dsn, "obs": obs, "obs_dsn": obs_db.dsn}
    finally:
        drop_run_database(db)
        drop_candidate_role(cand)
        drop_candidate_role(obs)


def candidate(root: pathlib.Path, name: str, body: str) -> str:
    """The synthetic reference-A candidate whose migrator expand first runs ``body``."""
    d = root / name
    shutil.copytree(GOOD, d)
    src = (d / "candidate.py").read_text(encoding="utf-8")
    src += ("\n\nclass Variant(Implementation):\n    def migrator_expand(self, db, a):\n"
            + "\n".join("        " + line for line in body.strip().splitlines())
            + "\n        return super().migrator_expand(db, a)\n")
    (d / "candidate.py").write_text(src, encoding="utf-8")
    m = json.loads((d / "gauntlet_candidate.json").read_text(encoding="utf-8"))
    m["name"], m["entry"]["class"] = name.replace("_", "-"), "Variant"
    (d / "gauntlet_candidate.json").write_text(json.dumps(m), encoding="utf-8")
    return register(d)["impl_id"]


def role_exists(name: str) -> bool:
    with psycopg.connect(PG_ADMIN_DSN, autocommit=True) as c:
        return c.execute("SELECT 1 FROM pg_roles WHERE rolname = %s", (name,)).fetchone() is not None


def drop_role(name: str) -> None:
    with psycopg.connect(PG_ADMIN_DSN, autocommit=True) as c:
        c.execute(sql.SQL("DROP ROLE IF EXISTS {}").format(sql.Identifier(name)))


def escalation_sql(marker: str) -> list[str]:
    """Replace orders with a view that calls a SECURITY INVOKER function creating a SUPERUSER role: whoever reads
    the view runs the function with their own privileges."""
    return ["ALTER TABLE orders RENAME TO orders_real",
            "CREATE FUNCTION public.gauntlet_escalate() RETURNS int LANGUAGE plpgsql AS "
            f"$f$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_catalog.pg_roles WHERE rolname = '{marker}') THEN "
            f"EXECUTE 'CREATE ROLE {marker} SUPERUSER'; END IF; RETURN 1; END $f$",
            "CREATE VIEW public.orders AS SELECT id, total, public.gauntlet_escalate() AS x FROM orders_real"]


# -- the role itself ---------------------------------------------------------------------------------------------
def test_observer_role_has_no_administrative_attributes_memberships_or_other_databases(run_roles):
    other = create_run_database()
    try:
        with psycopg.connect(PG_ADMIN_DSN, autocommit=True) as c:
            a = c.execute("SELECT rolsuper, rolcreaterole, rolcreatedb, rolreplication, rolbypassrls, rolinherit, "
                          "rolcanlogin FROM pg_roles WHERE rolname = %s", (run_roles["obs"],)).fetchone()
            assert a == (False, False, False, False, False, False, True)
            assert c.execute("SELECT count(*) FROM pg_auth_members m JOIN pg_roles r ON r.oid = m.member "
                             "WHERE r.rolname = %s", (run_roles["obs"],)).fetchone()[0] == 0
            priv = "SELECT has_database_privilege(%s, %s, 'CONNECT'), has_database_privilege(%s, %s, 'TEMPORARY')"
            assert c.execute(priv, (run_roles["obs"], run_roles["db"].name) * 2).fetchone() == (True, False)
            assert c.execute(priv, (run_roles["obs"], other.name) * 2).fetchone() == (False, False)
    finally:
        drop_run_database(other)


def test_observer_cannot_use_superuser_or_administrative_capabilities(run_roles):
    """Even if candidate code ran inside the observer's session, it would have none of these."""
    with psycopg.connect(PG_ADMIN_DSN, autocommit=True) as su:
        su_pid = su.info.backend_pid
        with psycopg.connect(run_roles["obs_dsn"], autocommit=True) as c:
            for stmt in (f"CREATE ROLE gauntlet_pwned_{uuid.uuid4().hex[:8]} SUPERUSER",
                         f"CREATE DATABASE gauntlet_pwned_{uuid.uuid4().hex[:8]}",
                         "ALTER SYSTEM SET work_mem = '64MB'",
                         "COPY (SELECT 1) TO PROGRAM 'echo gauntlet'",
                         "SELECT rolpassword FROM pg_catalog.pg_authid",
                         f"SELECT pg_catalog.pg_terminate_backend({su_pid})",
                         "CREATE TABLE public.gauntlet_obs_write (x int)",
                         "INSERT INTO public.orders (id, total) VALUES (99, 1)",
                         "CREATE TEMP TABLE gauntlet_obs_tmp (x int)"):
                with pytest.raises(psycopg.errors.InsufficientPrivilege):
                    c.execute(stmt)


def test_observer_capture_matches_the_superuser_capture(run_roles):
    with psycopg.connect(run_roles["cand_dsn"], autocommit=True) as c:  # mid-migration state, as the candidate
        c.execute("ALTER TABLE orders ADD COLUMN total_cents BIGINT")
        c.execute("UPDATE orders SET total_cents = (total * 100)::bigint WHERE id = 1")
        c.execute("CREATE FUNCTION public.t() RETURNS trigger LANGUAGE plpgsql AS $f$ BEGIN RETURN NEW; END $f$")
        c.execute("CREATE TRIGGER orders_t BEFORE UPDATE ON orders FOR EACH ROW EXECUTE FUNCTION public.t()")
    with open_observer(run_roles["obs_dsn"]) as obs, open_monitor(run_roles["db"].dsn) as su:
        assert capture(obs) == capture(su)


# -- end to end ----------------------------------------------------------------------------------------------------
def test_no_superuser_session_opens_in_a_candidate_run_database(env, monkeypatch):
    """Every connection the harness opens during an external run (including crash emulation, S04) is recorded:
    none targets the run database as the admin (superuser) user, and snapshots are read by the observer."""
    seen: list[tuple] = []
    real = psycopg.connect

    def recording(dsn="", *args, **kw):
        p = conninfo.conninfo_to_dict(dsn)
        seen.append((p.get("user"), p.get("dbname"), kw.get("application_name")))
        return real(dsn, *args, **kw)

    monkeypatch.setattr(psycopg, "connect", recording)
    impl = candidate(env, "plain", "pass")
    r = execute_run(S04, impl, 1, time_limit_s=180, isolation=ISO)
    assert r["verdict"] == "pass" and r["infra_error"] is None, r["infra_error"]
    in_run_db = [s for s in seen if s[1] == r["database"]]
    assert in_run_db and all(user != ADMIN_USER for user, _, _ in in_run_db), in_run_db
    assert (r["database_role"]["observer"], r["database"], "gauntlet:observer") in in_run_db
    assert any(app == "gauntlet:control" and db != r["database"] for _, db, app in seen)


def test_view_replacing_orders_is_not_read_and_cannot_escalate(env, run_roles):
    marker = f"gauntlet_pwned_{uuid.uuid4().hex[:10]}"
    # Positive control: the same objects, read by a superuser, DO create the superuser role.
    with psycopg.connect(run_roles["cand_dsn"], autocommit=True) as c:
        for stmt in escalation_sql(marker):
            c.execute(stmt)
        c.execute(sql.SQL("GRANT SELECT ON public.orders TO {}").format(sql.Identifier(run_roles["obs"])))
    try:
        with psycopg.connect(run_roles["db"].dsn, autocommit=True) as su:
            su.execute("SELECT * FROM public.orders").fetchall()
        assert role_exists(marker), "positive control: reading the view as a superuser must escalate"
    finally:
        drop_role(marker)
    # The observer reading the same view (if it ever did) cannot escalate either.
    with psycopg.connect(run_roles["obs_dsn"], autocommit=True) as obs:
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            obs.execute("SELECT * FROM public.orders").fetchall()
    assert not role_exists(marker)
    # End to end: a candidate doing this during its migration is never read and never escalates.
    impl = candidate(env, "escalator", "\n".join(f"db.execute({s!r})" for s in escalation_sql(marker)))
    r = execute_run(S01, impl, 1, time_limit_s=180, isolation=ISO)
    try:
        assert r["error_class"] == "candidate_execution_failure", r["infra_error"]
        assert "not a plain table" in r["infra_error"] and r["verdict"] != "pass"
        assert not role_exists(marker), "the harness executed candidate code with superuser rights"
    finally:
        drop_role(marker)


REVOKE = ("DO $d$ DECLARE g text; BEGIN FOR g IN SELECT DISTINCT grantee FROM information_schema.role_table_grants "
          "WHERE table_schema = 'public' AND table_name = 'orders' AND grantee <> current_user LOOP "
          "EXECUTE format('REVOKE ALL ON public.orders FROM %I', g); END LOOP; END $d$")


@pytest.mark.parametrize("name, stmts", [
    ("revoker", [REVOKE]),
    ("hider", ["ALTER TABLE orders ENABLE ROW LEVEL SECURITY", "ALTER TABLE orders FORCE ROW LEVEL SECURITY",
               "CREATE POLICY gauntlet_hide ON orders USING (false)"]),
])
def test_candidate_that_obstructs_observation_is_a_candidate_execution_failure(env, name, stmts):
    """Revoking the observer's access, or row-level security (an error under row_security = off, never silent
    filtering): a candidate execution failure, never a pass."""
    impl = candidate(env, name, "\n".join(f"db.execute({s!r})" for s in stmts))
    r = execute_run(S01, impl, 1, time_limit_s=180, isolation=ISO)
    assert r["error_class"] == "candidate_execution_failure", r["infra_error"]
    assert "observer" in r["infra_error"] and r["verdict"] != "pass"
    assert all(i["status"] == "infra_error" for i in r["invariants"])


DECOYS = ["CREATE SCHEMA gauntlet",  # the admin user's name: the "$user" entry of a default search_path
          "CREATE TABLE gauntlet.orders (id BIGINT, total NUMERIC(12,2))",
          "INSERT INTO gauntlet.orders VALUES (424242, 1.00)",
          "CREATE TABLE gauntlet.migration_state (id INT, stage TEXT)",
          "INSERT INTO gauntlet.migration_state VALUES (1, 'decoy')"]


def test_database_defaults_set_by_the_candidate_do_not_reach_the_observer(run_roles):
    """The candidate owns its database and can change its defaults for new sessions; the observer's startup
    options take precedence, and qualified names never resolve to the decoys."""
    with psycopg.connect(run_roles["cand_dsn"], autocommit=True) as c:
        for stmt in DECOYS:
            c.execute(stmt)
        c.execute(sql.SQL("ALTER DATABASE {} SET search_path = gauntlet, public").format(
            sql.Identifier(run_roles["db"].name)))
        c.execute(sql.SQL("ALTER DATABASE {} SET row_security = on").format(sql.Identifier(run_roles["db"].name)))
        c.execute(sql.SQL("GRANT USAGE ON SCHEMA gauntlet TO {}").format(sql.Identifier(run_roles["obs"])))
        c.execute(sql.SQL("GRANT SELECT ON ALL TABLES IN SCHEMA gauntlet TO {}").format(sql.Identifier(run_roles["obs"])))
    with open_observer(run_roles["obs_dsn"]) as obs:  # a NEW session, after the defaults changed
        assert obs.execute("SHOW search_path").fetchone()[0].replace(" ", "") == "pg_catalog,pg_temp"
        assert obs.execute("SHOW row_security").fetchone()[0] == "off"
        state = capture(obs)
    assert [row["id"] for row in state["rows"]] == [1, 2] and state["impl_reported_stage"] == "legacy"


def test_decoy_schema_does_not_change_what_is_observed(env, run_cache):
    """End to end: decoy tables in a schema named after the admin user do not affect the run, which is judged
    exactly as the reference implementation's."""
    impl = candidate(env, "shadower", "\n".join(f"db.execute({s!r})" for s in DECOYS))
    r = execute_run(S01, impl, 1, time_limit_s=180, isolation=ISO)
    ref = run_cache(S01, "ref_a_trigger", 1)
    assert r["verdict"] == "pass" and r["infra_error"] is None, r["infra_error"]
    assert all(424242 not in [row["id"] for row in s["rows"]] for s in r["snapshots"])
    assert [s["rows"] for s in r["snapshots"]] == [s["rows"] for s in ref["snapshots"]]
