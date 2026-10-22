# Task: an online money-column migration that is safe under a rolling deployment

You are writing the migration and the new application version (V2) for a service that stores orders in
PostgreSQL 16. The legacy application (V1, provided in `legacy.py`, which you must not change) stores an
order's total as `orders.total NUMERIC(12,2)`. The target schema stores it as
`orders.total_cents BIGINT` (exact cents) and eventually drops `total`.

During the migration, V1 and V2 instances run **at the same time** against the same database. Requests
can be retried with the same idempotency key, responses can be lost, processes (including the
migration worker) can be killed at any point and restarted, and steps may be executed again after they
completed.

## What to deliver

A directory containing `gauntlet_candidate.json` and Python source, as specified in the contract below.
Your class provides:

* V2 operations: `v2_create_order {order_id, total, key}`, `v2_set_total {order_id, total, key}`,
  `v2_add_charge {order_id, amount, key}` (returns the total after the charge), `v2_get_order {order_id}`.
  Amounts are decimal strings with exactly two decimals; results use the same format.
* Migration steps (run by a separate migrator process, in this order, each possibly interrupted, retried
  or repeated): `migrator_expand {}`, `migrator_backfill_batch {batch_size}` (returns `{"updated", "done"}`;
  called until `done`), `migrator_finalize {}`, `migrator_contract {}`.

Use only the `db` handle you are given (`query`, `one`, `execute`, `transaction`), `AppError` for
client-visible failures, and the helpers in `base.py` / `money.py`. Instance registration and V1 are
provided by the harness.

## Database

The database starts with exactly this schema (V1 and V2 instances register in `app_instances` while
serving and deregister when drained; `migration_state` is yours to use for migration control):

```sql
CREATE TABLE orders (
    id    BIGINT PRIMARY KEY,
    total NUMERIC(12,2) NOT NULL
);
CREATE TABLE processed_requests (
    key      TEXT PRIMARY KEY,
    response JSONB NOT NULL
);
CREATE TABLE app_instances (
    instance_id TEXT PRIMARY KEY,
    version     TEXT NOT NULL CHECK (version IN ('v1', 'v2'))
);
CREATE TABLE migration_state (
    id              INT PRIMARY KEY CHECK (id = 1),
    stage           TEXT NOT NULL,
    backfill_cursor BIGINT NOT NULL DEFAULT 0
);
INSERT INTO migration_state (id, stage) VALUES (1, 'legacy');
```

Your database role owns this database (it may run DDL) but is not a superuser.

## Requirements

1. An acknowledged write is never lost; amounts are exact to the cent.
2. While both columns exist and are both authoritative, they agree.
3. A retried request with the same key takes effect at most once.
4. Records never disappear or become corrupted; a request that failed is not applied.
5. Clients observe a consistent history (each read reflects the writes acknowledged before it).
6. An interrupted step can be resumed, a completed step can be executed again without changing data,
   and the backfill can be retried safely; the migration reaches a valid final state.
7. Schema changes must not break V1 instances that are still running.

Declare `"strategy": "A"` if one column is authoritative and kept in sync on every write, or `"B"` if V2
writes both columns and the migration reconciles them.

## Contract

See the "Candidate contract" section provided with this task (identical to docs/CANDIDATES.md §1).
