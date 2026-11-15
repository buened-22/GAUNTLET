# A-02 and FR-3: investigation and remediation plan

Investigation of two unresolved findings of [FINAL_RELEASE_READINESS_AUDIT.md](FINAL_RELEASE_READINESS_AUDIT.md):

* **A-02 / FR-1**: the monitor reads the candidate's run database with a PostgreSQL superuser session.
* **FR-3**: a candidate statement that exceeds the step timeout is classified `infrastructure`, not
  `candidate_execution_failure`.

| | |
|---|---|
| Base | commit `7c3ac9fd9c3d41fe31ac9acbd04ddf248d3a03d0`, branch `gauntlet/full-audit-hardening` |
| Date | 2026-11-05 |
| Method | Code reading only. No code, test or configuration change; no test run; no database was started. |
| Status | Investigation record, written before implementation; the plan text below is unchanged. **Since implemented** (2026-11-05): FR-3 in commit `58f628e` (with a distinct `candidate_statement_timeout` class instead of the `candidate_execution_failure` proposed in §2.3), and A-02 in the following commit (observer role `gauntlet_obs_*`, NOINHERIT, no `BYPASSRLS`; obstruction → `candidate_execution_failure`; no server-side `statement_timeout`; non-superuser admin left untested). |

Evidence labels used below:

* **[C]** confirmed by reading the code at the cited line.
* **[D]** documented PostgreSQL behaviour, not exercised here; each one is covered by a proposed test.
* **[H]** hypothesis; it needs a test or an experiment before the design relies on it.

---

## Part 1. A-02: the monitor's superuser session

### 1.1 Current state

**Where the superuser connection comes from**

* The default admin DSN is the bootstrap superuser `gauntlet` / `gauntlet` on the `postgres` database
  (`backend/gauntlet/config.py:14-17`) [C].
* `create_run_database` derives the run database's DSN from the admin DSN, so it carries the same user
  (`backend/gauntlet/pg/provision.py:82-84`) [C].
* For external candidates, `create_candidate_role` creates a per-run `NOSUPERUSER` role
  (`provision.py:96-116`) [C]:
  * it makes that role the owner of the run database (`provision.py:108`);
  * it revokes ALL on the database from PUBLIC (`provision.py:109`).

**What runs as the candidate role, and what runs as superuser**

* `apply_legacy_schema` runs as the candidate role (`runner.py:155` with `actor_dsn = role_db.dsn`, `runner.py:153`) [C]. The candidate
  role therefore owns:
  * the run database;
  * the `public` schema, through `pg_database_owner` on PostgreSQL 15 and later [D];
  * the tables `orders`, `processed_requests`, `app_instances` and `migration_state` [C/D].
* The monitor is then opened on `db.dsn`, the superuser DSN (`runner.py:156`, `open_monitor` at `runner.py:57-62`) [C].
  * It stays open for the whole run.
  * It sets only `lock_timeout` (`runner.py:61`) [C].

**How the controller uses the monitor connection** [C]

| Use | Location | Query |
|---|---|---|
| Snapshots, read by the oracle | `controller.py:453` → `observe.capture` (`observe.py:13-44`) | `information_schema.columns` (lines 14-19); `pg_trigger` with `'public.orders'::regclass` (21); **unqualified `FROM orders`** (29); **unqualified `FROM migration_state`** (39) |
| Lock probe | `controller.py:89-90` → `observe.blocking_pids` (`observe.py:47-49`) | `SELECT pg_blocking_pids(%s)`, unqualified |
| Liveness | `controller.py:282` → `observe.backend_alive` (`observe.py:52-53`) | `pg_stat_activity`, unqualified |
| Crash emulation | `controller.py:92-95` (passed to `ActorConnection.close`, `connection.py:186-189`) and `controller.py:284` | `SELECT pg_terminate_backend(%s)`, unqualified |
| Watchdog | `controller.py:498` | `cancel_safe()` |

**Ordering** [C]: the monitor connects at `runner.py:156`, after `apply_legacy_schema` (155) and before any actor
spawns. Candidate code first runs inside `ctl.execute` (`runner.py:168`). So when the monitor connects, the run
database contains only harness-created objects.

**Scope** [C]:

* Reference and mutant runs use the superuser DSN for actors too (`runner.py:150`). That is trusted code, so A-02
  is about external candidates only.
* The conventional suite (`conventional.py:34-39`) opens a superuser monitor and an unsandboxed pool, but it
  cannot run an external candidate: `loader.load` refuses one without a sandbox workspace
  (`candidates/loader.py:27-28`). **The conventional suite is therefore not an A-02 vector.**

### 1.2 What a candidate can do today (attack surface)

The candidate role owns the database, the schema and the tables. The following can make candidate-defined SQL run
inside the superuser monitor session:

1. **Replace `orders` with a view** [D].
   * In a migration step, `DROP TABLE orders` followed by `CREATE VIEW orders AS SELECT …, f(…)`.
   * A `SECURITY INVOKER` function referenced by a view runs as the *querying* user, here the superuser, when
     `capture` reads `FROM orders` (`observe.py:29`).
   * Inside that function, superuser rights allow, for example:
     * `CREATE ROLE … SUPERUSER`;
     * `COPY … TO PROGRAM`, which is OS command execution as the PostgreSQL service account;
     * `ALTER SYSTEM`;
     * reading `pg_authid`.
2. **Shadow names through `search_path`** [C/D].
   * The superuser's default `search_path` is `"$user", public`. `"$user"` resolves to a schema named `gauntlet`,
     after the admin user in `config.py:16`.
   * The candidate owns the database, so it can `CREATE SCHEMA gauntlet` and put `orders` / `migration_state`
     objects there that shadow `public`.
   * The unqualified function calls (`pg_blocking_pids`, `pg_terminate_backend`) are protected only by the
     implicit leading `pg_catalog` and by argument-type matching. That is the CVE-2018-1058 class of problem [D/H].
3. **`ALTER DATABASE <run db> SET …`** [D].
   * As owner, the candidate can change per-database defaults such as `search_path`.
   * These apply only to *new* sessions. The monitor connects before candidate code runs (§1.1), so today this
     does not reach it [C].
   * Any future reconnect of the monitor would be exposed to it.
4. **Row-level security** [D].
   * The superuser bypasses RLS, so policies cannot hide rows from today's monitor.
   * A restricted monitor would be subject to RLS. This is relevant to the fix (§1.4, Q4).

**Not vectors, and why:**

* Event triggers, base types and C-language functions all require superuser to create [D].
* `ON SELECT` rules on tables can no longer be created in PostgreSQL 16 (views only) [D].
* Triggers do not fire on `SELECT` [D].

**A related configuration that is untested [H]:** README "Any other PostgreSQL 16 server" allows a non-superuser
admin with `CREATEDB` and `CREATEROLE`.

* There the monitor is not a superuser, but it is a `CREATEROLE` role, which is still an administrative
  capability reachable through the same vectors.
* Whether crash emulation's `pg_terminate_backend` works there depends on PostgreSQL 16 role-membership
  semantics. It has never been tested.

### 1.3 Answers to the A-02 questions

**Q1. What exact privileges does the monitor need?**

The monitor's queries split into two groups with very different needs:

| Need | Queries | Minimum privilege |
|---|---|---|
| Observation, inside the run database | `capture` (`observe.py:13-44`) | `CONNECT` on the run database; `USAGE` on schema `public`; `SELECT` on `public.orders` and `public.migration_state`. `information_schema.columns` lists only columns the user holds a privilege on [D], so `SELECT` on `orders` is also what makes those rows visible. `pg_trigger` is readable by PUBLIC [D]. |
| Control, cluster-wide | `pg_blocking_pids`, `pg_stat_activity`, `pg_terminate_backend` | `pg_blocking_pids` and `pg_stat_activity` are readable by any role [D]; `pid`, `state` and `wait_event` are visible for other roles' sessions, while the query text is not [D]. `pg_terminate_backend` of the candidate's backend needs one of: superuser; privileges of the candidate role; or membership in `pg_signal_backend`, which cannot signal superuser backends [D]. |

None of these queries needs to run *inside* the run database with any privilege beyond reading. The control queries
work from any database in the cluster: the lock table and `pg_stat_activity` are cluster-wide [D].

**Q2. Can the monitor be kept from executing candidate-created functions, triggers or objects?**

Largely yes, by not *referencing* candidate objects through name resolution or code paths:

* Pin `search_path` to `pg_catalog, pg_temp` with `pg_temp` explicitly last, and schema-qualify every name
  (`public.orders`, `pg_catalog.pg_blocking_pids`). This removes shadowing [D].
* Before reading, require that `public.orders` and `public.migration_state` are plain tables
  (`pg_class.relkind = 'r'`). This removes views, foreign tables and partitioned tables, which can route reads
  elsewhere.
  * No reference, mutant or synthetic fixture renames or replaces these tables: grep found no
    `RENAME`, `VIEW` or `DROP TABLE` in `backend/gauntlet/impls` or `backend/tests/candidates` [C].
* Require that the column types are built-in: `id` is `int8`, `total` is `numeric`, `total_cents` is `int8`.
  * Domains, enums and composites use built-in I/O and run no candidate code on output [D].
  * New base types need superuser [D].
  * The check documents the assumption and detects surprises.
* Set `row_security = off` in the monitor session. A query that RLS would affect then **errors** instead of being
  silently filtered, and policy expressions are never evaluated [D].

What remains:

* Planner or executor paths that call user code during a plain `SELECT` on a plain table with built-in types are
  not known to exist [H]. A test (§1.6, T2/T3) must confirm that a marker function is never called.
* Even if one did exist, the code would run with the restricted monitor's privileges, not superuser. This is the
  real boundary (Q3).

**Q3. Can the monitor read what it needs without access to other databases or administrative capabilities?**

Yes, with a per-run role:

* `NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS`, `LOGIN`, `CONNECTION LIMIT 1` or 2.
* No membership in any role.
* `CONNECT` on its own run database only.
* `SELECT` on the observed tables.

Its reach outside the run database:

* PUBLIC defaults on databases that still grant CONNECT to PUBLIC: `postgres`, and on a shared server, other
  databases.
* It owns nothing and can create nothing in `postgres`: since PostgreSQL 15, `CREATE` on `public` is not granted to
  PUBLIC [D].

**`pg_read_all_data` is rejected.** It would survive a candidate's `REVOKE`, which is attractive, but it is
cluster-wide. [H, to confirm by test before deciding] It is expected to include catalog tables such as `pg_authid`.
Code running as the monitor could then read every role's password verifier, including the superuser's (and the
default password is `gauntlet`). That is a worse leak than the one being fixed.

**Q4. Risks: `SECURITY DEFINER`, ownership, schema privileges, `search_path`, temp objects, candidate objects**

| Topic | Risk with a restricted monitor | Treatment |
|---|---|---|
| `SECURITY DEFINER` | A candidate-owned definer function runs as the candidate, which has *fewer* rights than today's superuser. It is no escalation for the candidate. It is reachable only if the monitor references it, which the pinned path and qualification prevent. | Pinned `search_path`; qualified names. |
| `SECURITY INVOKER` and views | This is the actual A-02 vector: the code runs as the monitor. | `relkind = 'r'` check. Even if bypassed, the code runs with the monitor's few rights. |
| Ownership | The candidate owns the database, `public` and the tables. It can `REVOKE` the monitor's `SELECT` and `CONNECT`, which it was granted as owner. `CONNECT` matters only for new sessions. | Treat 42501 / 42P01 during a snapshot of an external run as **observation obstructed**: `candidate_execution_failure`, never a pass (§1.4 step 5). |
| Table recreation | A legitimate migration might recreate a table. The new table would lack the monitor's grant. | `ALTER DEFAULT PRIVILEGES FOR ROLE <cand> IN SCHEMA public GRANT SELECT ON TABLES TO <mon>`, issued as the candidate role before candidate code runs. A candidate can undo it, which leads to obstruction, not a pass. |
| Schema privileges | `public` is owned by `pg_database_owner`, which is the candidate. It can revoke `USAGE` from PUBLIC. | Grant `USAGE ON SCHEMA public` to the monitor explicitly; obstruction handling as above. |
| `search_path` and `ALTER DATABASE SET` | Per-database and per-role defaults apply at session start [D]. | Pass settings as **startup options** (`options=-c search_path=pg_catalog,pg_temp -c row_security=off -c lock_timeout=… -c statement_timeout=…`). Startup options override database and role defaults [D]. Connect before candidate code runs (already so, §1.1). Re-`SET` defensively and assert with `SHOW`. |
| Temporary objects | Temp schemas are per-session; the candidate cannot create objects in the monitor's `pg_temp` [D]. `pg_temp` is searched first for relations unless placed explicitly [D]. | Put `pg_temp` last explicitly. Do not grant `TEMPORARY` to the monitor. |
| RLS | A candidate can `ENABLE` / `FORCE ROW LEVEL SECURITY` with a hiding policy. | `row_security = off` makes this an error [D]. No `BYPASSRLS`, so policies are never evaluated silently. |
| Superuser elsewhere in the run | `pg_terminate_backend` and the probes run in the same session. | Move them to a **control connection** that never connects to the run database (§1.4). |

**Q5. Role setup and cleanup across supported PostgreSQL versions**

The target is PostgreSQL 16; other versions add a note (`runner.py:146-147`). The scheme mirrors the existing
candidate role:

1. In `create_candidate_role`, or a new `create_monitor_role`, using the admin connection to `postgres`:
   * `CREATE ROLE gauntlet_mon_<id> LOGIN PASSWORD <random> NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS CONNECTION LIMIT 2`;
   * `GRANT CONNECT ON DATABASE <run> TO gauntlet_mon_<id>`.

   `GRANT … ON DATABASE` can be issued from any database, as `provision.py:110` already does [C].
2. In `apply_legacy_schema`, running **as the candidate role** before candidate code runs:
   * `GRANT USAGE ON SCHEMA public TO mon`;
   * `GRANT SELECT ON public.orders, public.migration_state TO mon`;
   * `ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT SELECT ON TABLES TO mon`.

   No superuser session ever opens in the run database.
3. Cleanup, after `DROP DATABASE … WITH (FORCE)` (`provision.py:91`): `DROP ROLE IF EXISTS gauntlet_mon_<id>`,
   with the same best-effort pattern as `drop_candidate_role` (`provision.py:119-128`) and the runner's
   independent cleanup steps (`runner.py:202-221`).
   * Once the database is gone, the role holds no privileges anywhere [D], so `DROP ROLE` succeeds.
   * If the database drop failed, `DROP ROLE` fails with 2BP01 and is recorded as a cleanup error.
   * An optional orphan sweep for `gauntlet_mon_%` / `gauntlet_cand_%` roles whose database no longer exists
     is follow-up work; no sweep exists today [C].

Version notes:

* `pg_database_owner` owning `public` and `public` having no PUBLIC `CREATE` are PostgreSQL 15+ behaviours [D].
* On PostgreSQL 14 or earlier, `public` allows PUBLIC `CREATE` by default. The pinned `search_path` and qualified
  names are then essential, not only defence in depth.
* `WITH (FORCE)` needs PostgreSQL 13+ and is already relied on [C].
* In the non-superuser-admin configuration, PostgreSQL 16 `CREATEROLE` can create the monitor role. It needs no
  `BYPASSRLS` and no predefined-role grants, deliberately, so the same scheme works there [D/H].

**Q6. Tests that show the monitor cannot use superuser privileges** (details in §1.6)

* Role attributes and memberships are checked directly.
* A hostile synthetic candidate replaces `orders` with a view that calls a marker / escalation function. A positive
  control shows the same view escalates when read by a superuser in a scratch database.
* `SET ROLE` to the monitor proves `CREATE ROLE`, `COPY TO PROGRAM`, `ALTER SYSTEM` and `pg_authid` reads fail
  with 42501.
* No superuser session is ever opened on a candidate run database.

### 1.4 Recommended design (A-02)

1. **Split the monitor into two connections** (external runs).
   * **Observer**: the per-run restricted role on the run database. It is used only by `capture`.
   * **Control**: the admin connection on the **`postgres` maintenance database**, not the run database. It is
     used for `pg_blocking_pids`, `pg_stat_activity` and `pg_terminate_backend`, all schema-qualified, with
     `search_path = pg_catalog`.
   * The control connection still uses the admin role, but it never resolves a name inside a candidate-owned
     database. The candidate cannot create objects in `postgres` on PostgreSQL 15+, and the existing tests already
     verify that the candidate role cannot create tables in `postgres` (`docs/SECURITY_MODEL.md` Database row) [C].
2. **Harden `capture`**:
   * qualify every name (`public.orders`, `public.migration_state`, `pg_catalog.*`, `information_schema.*`);
   * check that `relkind = 'r'` and that the column types are built-in before reading rows.

   The output (columns, `not_null`, triggers, rows, stage) must stay byte-identical for every existing
   implementation. That is gated by the 180-cell frozen-evidence comparison.
3. **Session settings as startup options** for the observer:
   * `search_path=pg_catalog,pg_temp`, `row_security=off`;
   * `lock_timeout` (existing `MONITOR_LOCK_TIMEOUT_MS`), a `statement_timeout`;
   * `default_transaction_read_only=on`, as defence in depth only, because code in the session could undo it.

   Verify them with `SHOW` after connecting.
4. **No superuser session in the run database**: run `apply_legacy_schema` and the grants as the candidate role
   (already true for the schema, `runner.py:155`).
5. **Classify obstruction**: a snapshot of an external run that fails because the candidate made the observed state
   unreadable becomes `candidate_execution_failure` with a clear message, never a pass. Examples:
   * 42501 insufficient privilege;
   * 42P01 undefined table;
   * a `relkind` or type check failure;
   * an RLS error.

   A lock timeout stays `infrastructure`, as today (`controller.py:454-456`).

   This also covers an existing gap [C]: today a candidate that drops `orders` makes `capture` raise a plain
   `psycopg` error. Only `LockNotAvailable` is caught (`controller.py:454`), so it ends as `harness_exception`.
6. **Reference and mutant runs stay unchanged** in their identities: superuser actors, and the monitor on the run
   database. Two reasons:
   * a non-superuser control role could not terminate their superuser backends [D];
   * the frozen evidence must not move.

   The qualified `capture` queries apply to all runs and are covered by the 180-cell comparison.

**Alternatives considered**

| Alternative | Why weaker |
|---|---|
| Only `ALTER ROLE gauntlet NOSUPERUSER`, or revoking superuser from the admin | The admin must create and drop databases and roles. The monitor would then be a `CREATEDB` / `CREATEROLE` role, still administrative, and reachable through the same vectors. It does not address name resolution. |
| Observer with `pg_read_all_data` | It cannot be revoked by the candidate, but it is cluster-wide and [H] includes `pg_authid`, a credential leak. |
| Observer with `BYPASSRLS` | It hides RLS silently instead of surfacing it, and it needs a `BYPASSRLS`-holding grantor, which the non-superuser-admin configuration lacks. `row_security=off` is simpler and louder. |
| Observe **as the candidate role** | No new role, and candidate code in the session gains nothing. But the candidate controls its own role's defaults (`ALTER ROLE … SET`) and its own grants, and with `FORCE ROW LEVEL SECURITY` the owner is subject to its own policies. The observer would share the candidate's identity and connection limit. It is a viable fallback if per-run monitor roles prove impractical. |
| Single long-lived shared monitor role | Simpler cleanup, but it accumulates `CONNECT` on every concurrent run database, and a leaked password reaches all of them. Per-run roles match the existing candidate-role pattern. |
| Keep the superuser, only qualify names and check `relkind` | It removes the known vectors but keeps superuser one bug away. The boundary would rest on query-writing discipline, not on privileges. |
| `SET ROLE <mon>` inside the superuser session | The session user stays superuser: candidate code can `RESET ROLE` [D]. This is not a boundary. |

**Files likely to change**

| File | Change |
|---|---|
| `backend/gauntlet/pg/provision.py` | `create_monitor_role` / `drop_monitor_role` (or extend the candidate-role functions); grants in, or after, `apply_legacy_schema` as the candidate role |
| `backend/gauntlet/engine/runner.py` | Open the observer and control connections for external runs; cleanup step for the monitor role; obstruction classification |
| `backend/gauntlet/engine/observe.py` | Qualified queries, `relkind` / type checks, an `ObservationObstructed` exception |
| `backend/gauntlet/engine/controller.py` | Take a separate `control` connection (defaulting to `monitor` for back-compatibility, which keeps `conventional.py:38-39` working); route probe, liveness and terminate through it |
| `backend/tests/test_audit_hardening_pg.py` (or a new `test_monitor_role_pg.py`), `backend/tests/candidates/<new hostile fixture>/` | New tests |
| `docs/SECURITY_MODEL.md`, `docs/LIMITATIONS.md`, `README.md`, `SECURITY.md`, `docs/CANDIDATES.md`, `research/technical-report.md`, `docs/FINAL_RELEASE_READINESS_AUDIT.md` (follow-up note) | Update the A-02 disclosure to describe what remains |

No change to the oracle, scenarios, references, mutants or frozen evidence.

### 1.5 Compatibility (A-02)

* `capture` output must be identical, because snapshots feed the oracle and the outcome hashes. Qualifying names
  is expected to be semantically identical for every existing implementation, since all of them create their
  tables in `public` [H]. Gate: the 180/180 frozen-cell comparison must stay identical.
* `information_schema.columns` filters by the current user's privileges [D]. With `SELECT` on `public.orders`, every
  column is visible. A test must compare observer output against superuser output for the same state.
* There is one more connection per external run: the control connection, roughly one connect.
* Role names are limited to 63 bytes: `gauntlet_mon_` plus 12 hex characters fits [C, the same scheme as
  `provision.py:101`].
* PostgreSQL earlier than 15: see Q5.
* Non-superuser admin configuration: crash emulation through the control connection still needs the right to
  signal the candidate's backend there [H]. This is not tested today either.

### 1.6 Regression tests (A-02)

| # | Test | Shows |
|---|---|---|
| T1 | Monitor role attributes: `rolsuper`, `rolcreaterole`, `rolcreatedb`, `rolreplication`, `rolbypassrls` all false; no rows in `pg_auth_members` for it; `CONNECT` only on its run database (`has_database_privilege` over all databases) | Least privilege as configured |
| T2 | Hostile synthetic candidate: during the migration it replaces `orders` with a view calling a plpgsql `SECURITY INVOKER` function that inserts a marker row and attempts `CREATE ROLE … SUPERUSER`. **Positive control**: the same view, read by a superuser connection in a scratch database, *does* create the role. Under the fix, no marker, no role, run classified `candidate_execution_failure`, never pass. | The A-02 vector is closed, and the test can detect escalation |
| T3 | Shadowing: the candidate creates schema `gauntlet` (the admin's `"$user"`), an `orders` there, and `public.pg_blocking_pids(bigint)` / `public.pg_terminate_backend(bigint)` overloads with marker side effects. The snapshot equals the expected state; no marker. | Pinned path and qualification |
| T4 | `SET ROLE` to the monitor role in a scratch database: `CREATE ROLE`, `CREATE DATABASE`, `COPY … TO PROGRAM`, `ALTER SYSTEM`, `SELECT FROM pg_authid` and `pg_terminate_backend(<a superuser backend>)` each fail with 42501 | No administrative capability, even if code runs as the monitor |
| T5 | RLS: the candidate enables and forces RLS with a hiding policy. The snapshot raises obstruction (no silent filtering); the policy marker is never evaluated. | `row_security=off` |
| T6 | Revocation: the candidate revokes `SELECT` / `USAGE` from the monitor. Obstruction, `candidate_execution_failure`, never pass. | Obstruction classification |
| T7 | Legitimate recreation: the candidate recreates `migration_state` (or a helper table). The monitor can still read it through default privileges, and the run is judged normally. | No false obstruction |
| T8 | `ALTER DATABASE <run> SET search_path = evil` by the candidate. The monitor's `SHOW search_path` is unchanged. | Startup options win |
| T9 | Crash emulation (S04 path, and `test_closing_an_actor_session_rolls_back_and_releases_its_locks` at `test_audit_hardening_pg.py:175`) works through the control connection for a candidate run | Termination still works |
| T10 | No superuser session in the run database: record every `psycopg.connect` the harness makes during a candidate run and assert that none targets the run database as the admin user. Also sample `pg_stat_activity` for superuser sessions in that database. | The A-02 property itself |
| T11 | Role cleanup after success, candidate failure, run timeout and cancellation: extend the leak checks at `test_candidate_pg.py:50` and `test_readiness_audit.py:51` to `gauntlet_mon_%` | No leaks |
| T12 | Observer versus superuser `capture` output equality on a fixed state; 180-cell frozen-evidence comparison | No behaviour change |
| T13 | [H] check: `pg_read_all_data` grants `pg_authid` reads (documents why it was rejected; optional) | Design rationale |

### 1.7 Risks that remain after the A-02 fix

* **Process mode still has no boundary.** Native candidate code can read the admin DSN and credentials from the
  harness process's environment or memory (same OS user) and connect as superuser itself. The fix removes the
  *in-database* escalation path, not this one. It is closed only by `strict` isolation (FR-2) and non-default
  credentials.
* The admin control connection remains a superuser session, on `postgres`.
* A candidate can still obstruct observation. That becomes a recorded candidate failure, never a pass.
* Unknown read-time code paths [H] would run as the restricted monitor, not as superuser.
* The non-superuser-admin configuration remains untested.

### 1.8 Implementation order (A-02)

1. Write the tests first: T1, T4, and T2's positive control (they fail or are skipped against today's code where
   appropriate).
2. Role provisioning and cleanup in `provision.py`, then T1, T4 and T11.
3. Control connection in the controller and runner; route probe, liveness and terminate through it; then T9.
4. Observer connection with startup options; grants and default privileges as the candidate role; then T8 and T10.
5. Harden `capture` (qualification, `relkind` and type checks); then T3, T12 and the 180-cell comparison.
6. Obstruction classification; then T2, T5, T6 and T7.
7. Full suite, the 180-cell comparison, evidence checksums. Documentation update.

---

## Part 2. FR-3: classification of the step timeout

### 2.1 Current state

**Q1. Where the step timeout happens and which layer owns it** [C]

* It is a **harness-side wall-clock wait** in `ActorProxy._wait_stmt_done` (`backend/gauntlet/actor/proxy.py:398-436`).
  * `deadline = time.monotonic() + STEP_TIMEOUT_S` (`config.py:24`, default 20 s, `GAUNTLET_STEP_TIMEOUT_S`).
  * The loop waits 10 ms per iteration for the statement thread (`ActorConnection.wait`, `connection.py:159-168`).
  * Each iteration probes `pg_blocking_pids` through the monitor (`proxy.py:429-433`).
  * Past the deadline it raises a plain `InfrastructureError` (`proxy.py:434-436`).
* `execute_run` maps any `InfrastructureError` that is neither `RunTimeout`, `RunLimitExceeded`, `ScheduleError`
  nor a loaded `ActorFailure` to `error_class = "infrastructure"` (`runner.py:177-186`).
* The actor connection has **no server-side `statement_timeout`**. It sets only
  `lock_timeout = CHILD_LOCK_TIMEOUT_MS` (15 s) (`connection.py:82-88`, `config.py:25`).
* A statement waiting on a heavyweight lock is **not** subject to the deadline:
  * the probe sees blockers and the actor becomes BLOCKED (`proxy.py:429-433`);
  * the controller then waits or resolves it;
  * after unblocking, `try_unblock` starts a fresh 20 s deadline (`proxy.py:446-451`);
  * lock waits are ended server-side by `lock_timeout`. That arrives as SQLSTATE 55P03, an ordinary statement
    error delivered to the actor (`connection.py:149-153`).

So the 20 s path fires only for a statement that is running and not blocked by a heavyweight lock. Examples:

* `pg_sleep`;
* a large or expensive statement;
* waits that `pg_blocking_pids` does not report, such as I/O, LWLocks and client-socket waits [D];
* a stalled server, network or host.

**Neighbouring timeouts, for contrast** [C]

| Timeout | Location | Class today |
|---|---|---|
| Actor gives no gate or response within 20 s (Python CPU, a hang) | `_await_next` → `_recv(timeout=STEP_TIMEOUT_S)` (`proxy.py:363-364, 296-301`) | `ActorFailure(loaded)` → `candidate_execution_failure` for external runs |
| No hello within 60 s | `wait_hello` (`proxy.py:279`) | `ActorFailure` (load-dependent) |
| Result over budget | `proxy.py:422-424` | `ActorFailure(loaded)` → `candidate_execution_failure`; precedent for FR-3 |
| Protocol operation blocked more than 30 s | `controller.py:132-141` | `infrastructure` |
| Snapshot lock wait (monitor `lock_timeout`) | `controller.py:452-456` | `infrastructure` |
| Run wall-clock limit or watchdog | `runner.py:163-166, 179-182`; `controller.py:488-500` | `timeout`, which takes precedence over everything above |
| **Statement neither completed nor blocked within 20 s** | `proxy.py:434-436` | **`infrastructure` (FR-3)** |

**What happens after the timeout today** [C]

* The exception unwinds to the runner. The statement **keeps running on the server**: nothing cancels it at
  `proxy.py:434`.
* `ctl.shutdown()` (`runner.py:176`) calls `proxy.exit()`:
  * it sends `exit` to the actor process and waits up to 10 s, then kills it (`proxy.py:335-346`);
  * `_close_conn` → `ActorConnection.close(terminate=…)` then terminates the backend with
    `pg_terminate_backend` through the monitor (`connection.py:179-198`).
* Backend termination rolls back the open transaction and releases its locks [D]. The existing test
  `test_closing_an_actor_session_rolls_back_and_releases_its_locks` (`test_audit_hardening_pg.py:175`) covers this.
* No verdict is produced, and the run is never a pass.

**There is no existing test of this path**: grep found no `pg_sleep`, `STEP_TIMEOUT` or `statement_timeout` in
`backend/tests` [C]. The nearest are:

* `test_endless_candidate_is_stopped_by_the_timeout` (`test_candidate_pg.py:94-98`, Python-side loop, run limit);
* the watchdog tests (`test_watchdog.py`).

### 2.2 Answers to the FR-3 questions

**Q2. Can a candidate's over-long statement be told apart from connection, monitor or controller failures?**

Not from the current information: the loop records only "the deadline passed". A reliable distinction needs
*positive evidence* that the server was healthy and the statement itself was the slow part. Proposed evidence,
gathered when the deadline passes:

1. **Harness stall check** [C-feasible].
   * Record the largest gap between successive loop iterations. Normally it is about 10 ms plus one probe.
   * A gap of seconds means the harness process or the host stalled (sleep, suspension, heavy load: the FR-6
     scenario). Such a run is `infrastructure`.
2. **Server responsiveness and statement state**, from the control connection with a short `statement_timeout`:
   * `SELECT state, wait_event_type, wait_event, now() - query_start FROM pg_catalog.pg_stat_activity WHERE pid = …`.
   * If this query fails or times out, the server is unresponsive: `infrastructure`.
   * `state = 'active'` with server-side elapsed time of at least the step limit means the server has been
     executing this statement for the whole period.
   * `wait_event` is recorded for diagnosis, for example `PgSleep`, `IO`/`DataFileRead`, `LWLock`, `Client`
     [D, PostgreSQL 16 wait-event names].
3. **Cancel and observe** [D].
   * Send a cancel request (`ActorConnection.cancel`, `connection.py:171-177`) and wait a short grace period
     (proposed 5 s) for the statement thread.
   * **57014 `query_canceled` returned promptly**: the server was alive and ran the statement until cancelled.
     Together with 1 and 2, this is attributable to the candidate.
   * **Connection error** (psycopg `OperationalError`, SQLSTATE class 08, or 57P01 / 57P02 / 57P03): the server or
     connection failed. `infrastructure`.
   * **Cancel not honoured within the grace period**: `infrastructure`. The connection is then terminated as
     today.
   * **The statement completed in the race window**: the server was alive, and the statement crossed the time
     limit anyway. Attributed to the candidate.

Without 1 and 2, a 57014 after a cancel cannot prove the candidate was at fault. A server that was merely slow
under load would also honour the cancel. The stall check and the server-side elapsed time narrow this. Residual
ambiguity is unavoidable [H]: a host loaded enough to slow the server but not the harness loop can still look like
a slow statement. The class should therefore carry its evidence (§2.3) so a reviewer can judge.

The monitor and controller failures stay distinguishable because they are raised elsewhere: snapshot lock timeout
at `controller.py:452-456`, protocol block at `controller.py:132-141`, run limit in `runner.py`.

**Q3. Relevant PostgreSQL cancellation behaviour and SQLSTATEs** [D]

| SQLSTATE | Name | When |
|---|---|---|
| 57014 | `query_canceled` | Cancel request ("canceling statement due to user request") **and** `statement_timeout` ("… due to statement timeout"). The same code for both; only the message differs. |
| 55P03 | `lock_not_available` | `lock_timeout` (the actor's 15 s; the monitor's snapshot wait) |
| 40P01 | `deadlock_detected` | Server deadlock detection |
| 25P02 | `in_failed_sql_transaction` | Any statement after an error inside an explicit transaction, until `ROLLBACK` |
| 57P01 | `admin_shutdown` | `pg_terminate_backend` or server shutdown (FATAL; the connection ends) |
| 57P02 / 57P03 | `crash_shutdown` / `cannot_connect_now` | Server crash or recovery |
| 08000 / 08003 / 08006 | connection exceptions | Lost connection (psycopg may report `OperationalError` without a SQLSTATE) |
| 25P03 | `idle_in_transaction_session_timeout` | Not set today |

* A cancel request is asynchronous and best-effort: it is delivered over a new connection, and it may arrive after
  the statement finished, so it can cancel nothing [D].
* psycopg's `cancel_safe()` (used at `connection.py:175` and `controller.py:498`) sends it without blocking the
  connection's lock [D].

**Q4. Actor state, transaction state, locks and cleanup after a timeout**

* After a cancel inside an explicit transaction, the transaction is **aborted but still open**: it is in error
  status, and its locks are held until `ROLLBACK` or session end [D]. In autocommit, the statement's effects are
  rolled back [D].
* Recommended: end the actor exactly as for any other candidate execution failure.
  * Kill the actor process (`proxy.kill`, `proxy.py:316-325`).
  * Close its connection with `terminate` (`connection.py:179-198`). This rolls back, releases locks and frees the
    backend.
  * Do this *inside* the timeout handling, before raising, so cleanup does not depend on `exit()`'s 10 s wait at
    `proxy.py:337-341`.
* The run ends unjudged; there is no final snapshot.
* Cleanup then proceeds as today: pool, run database (`DROP … WITH (FORCE)`), roles, slot (`runner.py:198-221`).

**Q5. Structured result to the candidate, or classification by the harness?**

**The harness should classify it directly, and the candidate should receive nothing.** Reasons:

* **Determinism.** Delivering a 57014 into the candidate's control flow would make the logical trace and the outcome
  hashes depend on wall-clock timing. A candidate could also branch on it and turn a timing accident into a pass or
  fail.
  * Today's 55P03 delivery (`lock_timeout`) is already timing-dependent. It is pre-existing and outside this fix,
    noted in §2.5.
* **Precedent.** The result-budget failure (`proxy.py:422-424`) does exactly this: the statement is cancelled, the
  actor gets no result, and an `ActorFailure(loaded=…)` makes the run a `candidate_execution_failure` for external
  runs and `infrastructure` for trusted ones (`runner.py:185`).

### 2.3 Recommended design (FR-3)

1. Add `StatementTimeLimitExceeded(ActorFailure)` in `proxy.py`, carrying the evidence:
   * `server_elapsed_s`, `wait_event_type` / `wait_event`, `cancel_outcome`, `max_loop_gap_s`.
2. In `_wait_stmt_done`, when the deadline passes:
   1. compute the loop-gap stall indicator;
   2. query the control connection for the backend's activity, with a short timeout;
   3. cancel and wait for the grace period;
   4. close the connection with `terminate` and kill the actor;
   5. raise one of:
      * `StatementTimeLimitExceeded(loaded=self._loaded)` when the evidence shows a healthy server and harness
        (§2.2: no stall, query answered, cancel honoured with 57014 or completion);
      * `InfrastructureError` with the evidence in the message otherwise.
3. The runner needs **no new class**. `ActorFailure` with `loaded` already maps to `candidate_execution_failure`
   for external runs and `infrastructure` for trusted ones (`runner.py:185`).
   * The run-level `timeout` precedence (`runner.py:179-182`) is unchanged: if the run limit has also passed, the
     class is `timeout`.
   * Add the evidence to `run["notes"]` (or a `failure_detail` field) so reports show why.
4. Optionally, as defence in depth only, add a server-side `statement_timeout` (step limit plus margin) at actor
   connection start (`connection.py:85`).
   * It bounds runaway work if the harness itself stalls.
   * It **cannot be relied on**: the candidate's SQL runs in the same session and can
     `SET statement_timeout = 0` or call `set_config` [C: requests are not filtered by content beyond `classify`,
     `child.py`; D: the setting is user-settable].

**Alternatives considered**

| Alternative | Why weaker |
|---|---|
| Rely on a server-side `statement_timeout` and classify on 57014 | The candidate can disable it in its own session. 57014 does not distinguish a timeout from a cancel without matching message text. |
| Reclassify every step timeout of an external run as a candidate failure | It misattributes host and server stalls (the FR-6 class of event) to the candidate. That is the inverse error. |
| Deliver 57014 to the candidate and continue the run | Timing enters the logical trace and outcome hashes, breaking reproducibility; the candidate can exploit it. |
| Classify in the controller | The proxy owns the connection, the deadline and the evidence; the controller would need to re-derive them. The proxy-raises / runner-maps split matches the existing result-budget path. |
| A new `error_class` (for example `candidate_time_limit`) | It is more precise, but consumers enumerate classes: `execution.outcome_class` (`execution.py:27-31`), explore's `RETRYABLE` (`explore.py:53`), stored text (`store.py:59`). It can be added later. The evidence in notes is enough for now. |

**Files likely to change**

| File | Change |
|---|---|
| `backend/gauntlet/actor/proxy.py` | `StatementTimeLimitExceeded`; evidence collection, cancel and grace, and cleanup in `_wait_stmt_done` |
| `backend/gauntlet/actor/connection.py` | Optionally a helper that waits for the outcome after a cancel; optionally `statement_timeout` at connect |
| `backend/gauntlet/engine/controller.py` | Expose an activity query on the control connection to the proxy (as `_probe` is passed today, `controller.py:102-104`) |
| `backend/gauntlet/engine/runner.py` | Record the evidence in the run; no classification change needed |
| `backend/gauntlet/config.py` | `STEP_CANCEL_GRACE_S`, `STEP_STALL_GAP_S` (environment-overridable like the others) |
| `backend/tests/test_audit_hardening_pg.py` (or a new `test_step_timeout_pg.py`), a new synthetic slow-statement fixture | Tests |
| Docs (as for A-02) | Remove or adjust the timeout-classification disclosure |

### 2.4 Regression tests (FR-3)

All of these use a short `GAUNTLET_STEP_TIMEOUT_S` (for example 2 s) through `monkeypatch`, to keep them fast.

| # | Test | Expected |
|---|---|---|
| U1 | Synthetic candidate whose migration step runs `SELECT pg_sleep(30)` | `candidate_execution_failure`; evidence shows `PgSleep` and a 57014 cancel; never a pass; total duration bounded |
| U2 | The same inside an explicit transaction that first takes a row lock (`BEGIN; UPDATE orders …; SELECT pg_sleep(30)`) | Afterwards: no backend left for the actor (`pg_stat_activity`); another session can update that row immediately (lock released); the row is unchanged (rolled back) |
| U3 | Built-in (trusted) implementation with an injected slow statement (test-only `hang_inject` style, cf. `backend/tests/hang_inject/`) | `infrastructure` (trusted code is never "candidate"), with evidence |
| U4 | Harness stall: monkeypatch the wait loop so one iteration sleeps longer than `STEP_STALL_GAP_S` | `infrastructure`, never `candidate_execution_failure` |
| U5 | Unresponsive server: monkeypatch the activity query to raise or time out | `infrastructure` |
| U6 | Cancel not honoured: monkeypatch `cancel` to a no-op so the statement outlives the grace period | `infrastructure`; the connection is still terminated; no leaked backend |
| U7 | Connection lost mid-statement (terminate the backend from the test while it sleeps) | `infrastructure` (57P01 / `OperationalError`) |
| U8 | Lock-blocked statement longer than the step limit | Still BLOCKED and handled by the controller, **not** a step timeout (regression guard for `proxy.py:429-433`) |
| U9 | Run limit shorter than the step limit | `timeout` keeps precedence (`runner.py:179-182`) |
| U10 | After U1, in the same test session, a fresh normal candidate run passes | No leaked state between runs |
| U11 | Exploration retry: a U1-style schedule in `explore` is not retried (`RETRYABLE`, `explore.py:53`) | Budget is not wasted on a deterministic candidate failure |

### 2.5 Compatibility (FR-3)

* **Frozen evidence**: references and mutants never reach the new classification. `runner.py:185` requires
  `external`, and no frozen run hit the step timeout, since all 180 completed. The 180-cell comparison should
  still be run.
* **Exploration**: external candidates' step timeouts are no longer retried as `infrastructure`
  (`explore.py:51-53`). This is intended. Minimization and differential testing treat any `infra_error` as
  unjudged or inconclusive, unchanged (`minimize.py:155,309`; `differential.py:133-141`).
* **Duration**: a timeout now adds up to the cancel grace period (5 s), but it removes the 10 s
  `exit()` wait (`proxy.py:337-341`), so it is likely a net reduction [H].
* **Pre-existing, out of scope**:
  * the actor's `lock_timeout` 55P03 delivered to candidates is timing-dependent;
  * another actor of the same candidate role can `pg_cancel_backend` its sibling, and the resulting 57014 is
    delivered as an ordinary error.

  Both are worth recording in LIMITATIONS, not fixing here.
* **Platform**: the evidence queries are standard PostgreSQL; the loop-gap measure uses `time.monotonic()`, which is
  available everywhere.

### 2.6 Risks that remain after the FR-3 fix

* Residual ambiguity on an overloaded host (§2.2). It is mitigated by recorded evidence, not eliminated.
* A candidate can still make a run unjudged by being slow. That is now correctly attributed, never a pass.
* The thresholds (grace period, stall gap) are heuristics. Too tight a stall gap would misclassify candidate faults
  as infrastructure. The safe direction is the current one.

### 2.7 Implementation order (FR-3)

1. U8 and U9 as regression guards against current behaviour (they should pass today).
2. U1 written against today's code: it documents the current `infrastructure` result, then flips.
3. Evidence collection (stall gap, activity query via the control connection), cancel and grace, and cleanup in
   `_wait_stmt_done`.
4. `StatementTimeLimitExceeded`; evidence recorded in the run.
5. U1–U7, U10, U11.
6. Full suite, the 180-cell comparison, documentation update.

---

## Part 3. Combined plan

**Dependency.** The fixes are mostly independent, with one shared piece: the **control connection** introduced by
A-02 (§1.4 step 1).

* FR-3's evidence query and its terminate path are cleaner on that connection.
* FR-3 could use the existing monitor in the meantime. But once A-02 lands, the monitor becomes a restricted
  observer that cannot terminate backends, so FR-3 must then move to the control connection anyway.

**Recommended sequence**

1. **Shared groundwork**: introduce the control connection (admin, `postgres` database, pinned path, qualified
   catalog functions) and route `pg_blocking_pids`, `pg_stat_activity` and `pg_terminate_backend` through it, with
   no change of privilege yet. Full suite and 180-cell comparison.
2. **FR-3** on top of it (§2.7). It is smaller and lower risk: it changes classification only, and only for external
   candidates.
3. **A-02** (§1.8): monitor role, observer connection, hardened `capture`, obstruction classification.
4. Final: full suite, 180-cell comparison, evidence checksums, documentation update (remove the FR-3 disclosure and
   rewrite the A-02 disclosure to the residual risks in §1.7), and a follow-up note in the audit report.

Each step can be a separate reviewable commit. Steps 2 and 3 can be swapped if A-02 is the priority. The only
requirement is that step 1 comes first.

**Decisions needed before implementation**

1. Is the per-run observer role (recommended) accepted over "observe as the candidate role" (the simpler
   fallback)?
2. Should obstruction of observation be `candidate_execution_failure` (recommended), or a new class?
3. The FR-3 thresholds: cancel grace period (proposed 5 s) and stall gap (proposed 2 s).
4. Should the optional server-side `statement_timeout` (defence in depth only) be added?
5. Should A-02 hardening extend to the non-superuser-admin configuration's tests now, or stay documented as
   untested?

**What will still not be true after both fixes**

* `process` mode is still not a security boundary (FR-2).
* Native candidate code can still read the admin credentials from the harness process and connect as superuser.
* Untrusted or AI-generated code must still not be run outside a verified `strict` (container/VM) boundary.
