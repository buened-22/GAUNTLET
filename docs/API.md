# API reference

`python -m gauntlet serve` starts a local HTTP API (FastAPI) on `http://127.0.0.1:8000`. The dashboard uses
it; you can also call it directly. Interactive documentation generated from the code is served at
`http://127.0.0.1:8000/docs`, and it is the authoritative list of request and response fields. This page
describes the endpoints and their behaviour as implemented in `backend/gauntlet/api/app.py` and
`backend/gauntlet/api/analyses.py`.

> [!WARNING]
> **The API has no authentication and is for local use only.** It binds to `127.0.0.1` by default, and
> `gauntlet serve` prints a warning if `--host` is given a non-loopback address. Never expose it on a
> network: anyone who can reach the port can use it. Through `POST /api/evaluations` it can start a
> candidate evaluation, including the trust declaration that `process` mode requires.

## Request checks (not authentication)

The API refuses requests that a web page in the operator's browser could relay to it
(`backend/gauntlet/api/local_guard.py`; [SECURITY_MODEL.md](SECURITY_MODEL.md#the-api-and-dashboard)):

* a request whose `Host` header is not a loopback name (`127.0.0.1`, `localhost`, `::1`, plus any names in
  `GAUNTLET_API_ALLOWED_HOSTS`) is refused with **403** (DNS rebinding);
* a state-changing request whose `Origin` is neither the local dashboard (`http://localhost:5173`,
  `http://127.0.0.1:5173`, plus `GAUNTLET_API_ALLOWED_ORIGINS`) nor the server itself is refused with
  **403** (cross-site request);
* a request body that is not `application/json` is refused with **415**.

These checks stop a browser from being used as a relay. Any local process, and anyone who can reach the
port directly, can still use the API.

## Execution model

* `POST` endpoints that start work return immediately with an id and `"status": "queued"` (HTTP 202); poll
  the matching `GET` endpoint for the result.
* Runs (`POST /api/runs`) execute one at a time on one worker. Analyses (differential comparisons,
  explorations, minimizations, replays, candidate evaluations) execute one at a time on a second worker, so
  a run and an analysis can execute at the same time.
* Runs left queued or running by a previous server process are marked as failed to start when the server
  starts again.
* A run that is not completed cannot be read through the endpoints that need its full record
  (`/trace`, `/invariants`, `/report`, `/compare`): they return **409**. Unknown ids return **404**;
  invalid requests return **422**.

## Example

```bash
curl http://127.0.0.1:8000/api/health
curl -X POST http://127.0.0.1:8000/api/runs -H "Content-Type: application/json" \
     -d '{"scenario_id": "S02_backfill_vs_legacy_writes", "impl_id": "m1_stale_backfill", "seed": 1}'
# -> {"id": "<run_id>", "status": "queued"}
curl http://127.0.0.1:8000/api/runs/<run_id>               # status, verdict, hashes, metrics
curl http://127.0.0.1:8000/api/runs/<run_id>/invariants    # the 12 invariant results with evidence
curl http://127.0.0.1:8000/api/runs/<run_id>/report        # Markdown report
curl -X POST http://127.0.0.1:8000/api/differential -H "Content-Type: application/json" \
     -d '{"impl_ids": ["ref_a_trigger", "m1_stale_backfill"], "scenario_id": "S02_backfill_vs_legacy_writes", "seed": 1}'
curl http://127.0.0.1:8000/api/differential/<comparison_id>
```

## Endpoints

### Status and catalog

| Method and path | Returns |
|---|---|
| `GET /api/health` | `ok` and the PostgreSQL server version, or the connection error |
| `GET /api/meta` | Benchmark version, catalog counts, latest run, latest benchmark headline, number of active runs |
| `GET /api/scenarios`, `GET /api/scenarios/{scenario_id}` | Scenarios (S01–S06) |
| `GET /api/implementations` | References and mutants |
| `GET /api/invariants` | The 12 invariants |
| `GET /api/faults` | The fault catalog |
| `GET /api/fixtures` | Exploration fixtures such as `F01_backfill_race_probe` |

### Runs and benchmarks

| Method and path | Behaviour |
|---|---|
| `POST /api/runs` | Body `{"scenario_id", "impl_id", "seed"}` (seed defaults to 1). Queues one run; 202 |
| `GET /api/runs` | Persisted runs; query `limit` (default 200), `scenario_id`, `impl_id`, `benchmark_id` |
| `GET /api/runs/{run_id}` | One run without its event trace and ledger |
| `GET /api/runs/{run_id}/trace` | Events, attempts, snapshots, directives and decisions (completed runs only) |
| `GET /api/runs/{run_id}/invariants` | Invariant results with evidence (completed runs only) |
| `GET /api/runs/{run_id}/report` | Markdown report (plain text; completed runs only) |
| `POST /api/runs/{run_id}/replay` | Re-executes a completed run's stored schedule and compares hashes; 202, poll `GET /api/replays/{id}` |
| `GET /api/compare?a=…&b=…` | Comparison of two completed runs |
| `GET /api/benchmarks` | Recorded benchmarks |
| `GET /api/benchmarks/latest`, `GET /api/benchmarks/latest/report` | The latest completed benchmark and its Markdown report; 404 if none has run |

### Analyses

| Method and path | Behaviour |
|---|---|
| `GET /api/analyses` | Recent analyses of every kind; query `kind`, `limit` |
| `POST /api/analyses/{id}/cancel` | Requests cancellation of a queued or running job; 409 for kinds that cannot be cancelled |
| `POST /api/differential` | Body `{"impl_ids" (2–10), "scenario_id", "seed", "schedule_from_run"?, "time_limit_s"?}`; 202 |
| `GET /api/differential/{id}` | Result and classification |
| `GET /api/differential/rules` | Normalization rules and the four classifications |
| `POST /api/differential/{id}/replay` | Replays a completed comparison; 202, poll `GET /api/replays/{id}` |
| `POST /api/explorations` | Body `{"scenario_id", "impl_id", "seed", "directive_index"?, "limits"?}`; 202 |
| `GET /api/explorations/{id}` | Result |
| `GET /api/explorations/{id}/schedules/{n}/export` | One executed schedule with its run, verdict and trace |
| `POST /api/minimizations` | Body `{"run_id", "invariant"?, "budget"?}`; the run must have a confirmed invariant violation (otherwise 422); 202 |
| `GET /api/minimizations/{id}` | Result |
| `GET /api/replays/{id}` | Result of a run or comparison replay |

There is no `GET` list endpoint per analysis kind; use `GET /api/analyses?kind=…`. Limits and budgets, with
their defaults and bounds, are in the interactive documentation and in [ANALYSES.md](ANALYSES.md).

### Experiments

| Method and path | Behaviour |
|---|---|
| `POST /api/experiments` | Body `{"config": {…}}`; creates an experiment; 201 |
| `GET /api/experiments/{id}`, `PUT /api/experiments/{id}` | Read; revise with `{"changes", "reason"?}` |
| `POST /api/experiments/{id}/report`, `GET /api/experiments/{id}/report.md` | Generate (and record) a report; read it as Markdown without recording |
| `GET /api/experiments/{id}/verify` | Re-hashes the evidence named by the latest report's manifest |
| `GET /api/experiments/{id}/export` | Export bundle |

### External candidates

| Method and path | Behaviour |
|---|---|
| `GET /api/isolation` | Isolation capabilities on this host (`strict` is reported unavailable) |
| `GET /api/candidates`, `GET /api/candidates/{id}` | Registered candidates; one candidate with a fresh snapshot-integrity check |
| `POST /api/evaluations` | Body `{"candidate_id", "scenarios"?, "seeds"?, "references"?, "isolation"?, …}`; 202. `process` isolation requires `"isolation": {"trusted_environment": true}`, which is **not** a security boundary; `strict` fails closed |
| `GET /api/evaluations/{id}`, `GET /api/evaluations/{id}/summary.md` | Result; Markdown summary (409 until available) |

Candidates cannot be registered through the API: registration from a directory is a command-line operation
(`python -m gauntlet candidate register`), and the API never accepts host file-system paths
([CANDIDATES.md](CANDIDATES.md)).
