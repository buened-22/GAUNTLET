# Architecture

## Components

```
                        ┌──────────────────────────── harness process ─────────────────────────────┐
 scenario.build(seed) ─►│ schedule definition (directives, ops, initial state) ── sha256 ─► schedule_hash
                        │            │                                                              │
                        │            ▼                                                              │
                        │   Controller (engine/controller.py), single-threaded                      │
                        │     • grants one SQL statement at a time ──────────────┐                  │
                        │     • injects faults (pause / kill / drop response)    │ JSON-lines pipes │
                        │     • appends to Ledger (client view) and event trace  │                  │
                        │     • monitor connection: pg_blocking_pids, snapshots* │                  │
                        │            │                                            │                 │
                        │            ▼                                            ▼                 │
                        │   RunOracle (oracle/) ◄── ledger + raw snapshots   ActorProxy × N         │
                        │     linearizability, S3/S5, R1–R4, C1                   │                 │
                        │            │                                            │                 │
                        │            ▼                                            │                 │
                        │   Store (SQLite) ◄── run record (trace, ledger, snapshots, results, hashes)
                        └─────────────────────────────────────────────────────────┼─────────────────┘
                                                                                  │
                    ┌─────────────── actor subprocess (one per actor incarnation) ▼──────────────┐
                    │ actor/child.py: loads the implementation by id; GatedDb is only a          │
                    │ request channel. The actor has NO database connection: the harness         │
                    │ (actor/connection.py) executes each statement after it is granted.         │
                    │ Implementation code (impls/*) runs ONLY here.                               │
                    └──────────────────────────────────────┬─────────────────────────────────────┘
                                                           ▼
                                  PostgreSQL 16, one fresh database per run
```

\* For external candidates (audit finding A-02), snapshots are read by a per-run non-superuser observer role,
and `pg_blocking_pids`, liveness checks and backend termination run on a separate control connection to the
maintenance database; see [SECURITY_MODEL.md](SECURITY_MODEL.md).

The FastAPI app (`api/app.py`) wraps the same runner. It runs one execution at a time on a
background worker and reads persisted results. The React dashboard (`frontend/`) only talks to the
API.

## Module boundaries

| Concern | Module |
|---|---|
| Scenario definitions and schedule DSL | `scenarios/catalog.py`, `scenarios/script.py` |
| Event scheduling and fault injection | `engine/controller.py` (directives), `actor/proxy.py` (gates, lock-wait detection) |
| Application and migration operations | `impls/legacy.py` (V1), `impls/reference_a.py`, `impls/reference_b.py` |
| Mutant registry | `impls/mutants.py`, `impls/registry.py` |
| Event ledger | `oracle/ledger.py` |
| Independent oracle | `oracle/linearizability.py`, `oracle/checker.py`, `oracle/invariants.py` |
| Raw observation | `engine/observe.py` |
| Benchmark execution | `engine/runner.py` (one run), `bench.py` (matrix, reproducibility, mutant validation) |
| Conventional baseline tests | `conventional.py` |
| Result persistence | `store.py` (SQLite) |
| Report generation | `report.py` |
| Run comparison | `compare.py` |
| API | `api/app.py` |
| Database provisioning | `pg/provision.py` (per-run DB, harness-owned P0 schema), `pg/localpg.py` (no-Docker cluster) |

## Data flow of one run

1. **Schedule.** `schedule_definition(scenario, seed)` produces the directive list, the logical
   operations and the initial orders. Its canonical JSON hash is the `schedule_hash`. It depends
   only on (scenario, seed, benchmark version).
2. **Provisioning.** The harness creates a fresh database, `gauntlet_run_<random>`, applies the
   harness-owned legacy schema and seeds the initial orders. The harness owns this schema, so every
   implementation starts from an identical state.
3. **Process pool.** One fresh process is started per `spawn` / `restart` directive, in parallel,
   so that Python start-up overlaps. Each incarnation is still its own OS process.
4. **Execution.** The controller interprets directives:
   * `call` appends an invocation to the ledger and sends the operation to the actor.
   * Each `grant` lets exactly one statement run.
   * A delivered response appends a completion. A dropped response, or a kill, appends an
     `uncertain` completion.
   * After every step that can release locks, blocked actors are resolved deterministically in
     name order.
5. **Settle.** Any operation still in flight is driven to completion, round-robin. If something
   cannot complete, it is recorded as `uncertain/incomplete` and the run is marked inconclusive.
   A final snapshot is then taken.
6. **Oracle.** `RunOracle` evaluates the 12 invariants from the ledger and the snapshots.
7. **Hashes.**
   * `trace_hash`: the event trace with timing and OS identifiers removed.
   * `outcome_hash`: attempt outcomes, invariant statuses and the final rows.
8. **Persistence.** The full record goes to SQLite and the database is dropped.

## Key design decisions and trade-offs

**Statement-level gating instead of instrumented implementation code.** Implementations call an
ordinary `Db` API (`query`, `execute`, `transaction`). The gate sits underneath that API, so fault
points like "before the first write" or "after the first commit" are *black-box*. The same
schedule discriminates a single-transaction reference from a two-transaction mutant without
knowing either one's code. The trade-off is granularity. Interleavings inside a single statement,
for example inside a trigger, are not explored.

**A separate OS process per actor.** Crashes are real process kills; the harness then ends that actor's
database session (it owns the connection), so PostgreSQL rolls back exactly as for a dropped connection. It also means implementation code never runs in the harness or
API process. That separation is a prerequisite for isolating externally generated solutions, but it is
**not** itself a security boundary: an actor process runs as the same operating-system user as the harness
(see [SECURITY_MODEL.md](SECURITY_MODEL.md)). The
trade-off is about 1.4 s of Python start-up per process on Windows, mitigated by the warm pool.

**Logical lock-wait detection.** A statement is classified as *blocked* when
`pg_blocking_pids(backend)` is non-empty. That is a property of database state, not a timeout.
When a lock holder commits or dies, the controller waits for the waiter to finish before taking
its next step. As a result, runs with lock waits reproduce the same logical trace (verified in
`docs/RESULTS.md`). Remaining nondeterminism: PostgreSQL's choice of deadlock victim, and anything
else timing-dependent inside the server. No current scenario deadlocks.

**Deterministic schedule versus observed execution.** The directive list is fixed by the seed. The
statement-level trace depends on the implementation, but given the same implementation, the same
PostgreSQL version and the same configuration, it was identical on every re-run measured. Wall
times are recorded (`t_ms`, `duration_ms`) but excluded from every hash.

**Linearizability as the core safety oracle.** Checking final totals alone would miss stale reads
and visible-then-lost writes, and would require trusting the implementation's own idea of "final".
A per-order register/counter model with real-time constraints is a standard, implementation-agnostic
correctness criterion. Diagnosis through relaxed models keeps S1, S2, S4 and S6 distinct without
special-casing any mutant.

**Strategy contracts in the oracle.** Whether the two columns must agree is a protocol property
(A: always after expand; B: only after finalize). The oracle encodes each strategy's *declared*
contract, which is documented in `docs/INVARIANTS.md`. It never reads that information from the
implementation.

**SQLite for results, PostgreSQL for the system under test.** This keeps evaluation data separate
from the database being tortured, and makes results portable.

## Migration strategies (reference implementations)

**Strategy A: trigger synchronisation (`ref_a_trigger`).**

* *Expand.* A nullable `total_cents` column, plus a `BEFORE INSERT OR UPDATE` trigger that derives
  whichever column the writer did not set (and rejects writes that set both columns
  inconsistently).
* *Backfill.* Cursor-based batches. Each batch is one transaction that computes
  `total_cents = total * 100` *inside the UPDATE statement*, so it always uses the row's current
  version.
* *Finalize.* Refuses unless the backfill is complete, no v1 instance is registered, and no row is
  NULL or disagrees. It then sets NOT NULL and switches authority.
* *Contract.* Refuses while v1 is registered, then drops the trigger and `total`.
* *Idempotency.* Every step is one transaction and is idempotent.
* *Assumptions.* All writes go through the database; instances register before serving.
* *Trade-off.* Robust to legacy writes without changing legacy code, but it puts business logic in
  the database and adds write amplification during the transition.

**Strategy B: atomic dual-write plus post-drain reconciliation (`ref_b_dualwrite`).**

* *During coexistence.* `total` stays authoritative. V2 writes both columns in a single statement,
  inside one transaction.
* *Finalize.* Runs only after v1 has drained. It reconciles every row, verifies, sets NOT NULL, and
  switches authority. V2 keeps dual-writing atomically until contract, so `total` remains a
  faithful mirror (rollback safety).
* *Assumptions.* No legacy write can happen after reconciliation (enforced by the drain check), and
  every post-finalize writer writes both columns atomically.
* *Trade-off.* No database triggers, but correctness rests on the reconciliation pass and the drain
  check. The benchmark shows those are exactly the steps whose omission is caught (m8).
