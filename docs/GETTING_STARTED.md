# Getting started

Install GAUNTLET, start PostgreSQL 16, run a first comparison and open the dashboard. For a clean-checkout
reproduction of the recorded results, follow [REPRODUCIBILITY.md](REPRODUCIBILITY.md) instead.

> [!WARNING]
> GAUNTLET does not sandbox untrusted code. Nothing on this page executes candidate code, but before you
> evaluate any external candidate read [../SECURITY.md](../SECURITY.md): `process` mode is a development
> sandbox for code you trust, and `strict` (container / VM) isolation is not implemented and always refuses
> to run.

## Requirements and platforms

Python ≥ 3.11 (developed on 3.14), Node ≥ 18 (developed on 24) for the dashboard and for the portable
PostgreSQL download, and a PostgreSQL 16 server.

**Windows 11** is where the benchmark and every recorded result were produced. The full test suite and the
demonstration script also pass in continuous integration on **Windows Server 2025** (Python 3.14, the
portable PostgreSQL 16.14) and on **Ubuntu 26.04.1** (Python 3.11 and 3.14, PostgreSQL 16 from
`docker-compose.yml`). The benchmark itself has not been re-executed on Linux, so identical hashes there are
not established. **macOS** has not been run at all. On Linux the tests that check the enforcement of
`process` mode's resource limits are skipped: those limits are applied there but **not verified**, and the
process-count limit is not enforced.

## 1. Install

All commands are run from the repository root unless a step says otherwise.

```bash
python -m venv .venv
# Windows (Git Bash): source .venv/Scripts/activate    Windows (cmd): .venv\Scripts\activate
# macOS/Linux:        source .venv/bin/activate
pip install -c backend/constraints.txt -e "backend[dev]"
```

`backend/constraints.txt` pins the exact dependency versions that the tests, the continuous-integration
jobs and the reproduction instructions use. Installing without it (`pip install -e "backend[dev]"`) resolves
newer versions that have not been tested.

## 2. Start PostgreSQL 16

**Option 1, no Docker** (what the results were produced with):

```bash
python -m gauntlet pg install   # downloads portable PostgreSQL 16.14 binaries via npm into .gauntlet/pgdist
python -m gauntlet pg init      # creates .gauntlet/pgdata (user gauntlet / password gauntlet, port 55432)
python -m gauntlet pg start
python -m gauntlet pg check     # prints server version
```

`pg stop` and `pg status` are also available. You can point `GAUNTLET_PG_BIN` at an existing PostgreSQL 16
`bin/` directory instead of downloading binaries.

**Option 2, Docker:**

```bash
docker compose up -d            # same port and credentials as option 1
```

Docker was not available on the development machine. This path is exercised by the Linux
continuous-integration jobs, which start PostgreSQL 16 from this file and run the full test suite against it.

**Any other PostgreSQL 16 server:** set `GAUNTLET_PG_DSN=postgresql://user:pass@host:port/postgres`. The user
needs `CREATEDB`, because each run creates and drops its own database, and `CREATEROLE` for external
candidates: the harness connects on the candidate's behalf as a per-run restricted role, and reads snapshots
as a per-run observer role. A non-superuser admin has not been tested.

The default credentials (`gauntlet` / `gauntlet`, a **superuser**, on `127.0.0.1:55432`) are a published
development default, not a secret. Use them only for a server bound to `127.0.0.1` on a machine you do not
share; see [../SECURITY.md](../SECURITY.md).

## 3. First run

```bash
export GAUNTLET_RESULTS_DB="$PWD/.gauntlet/demo.sqlite3"    # scratch results database
cd backend
python -m gauntlet diff --impls ref_a_trigger,m1_stale_backfill --scenario S02_backfill_vs_legacy_writes --seed 1
# -> CONFIRMED_VIOLATION  behaviour differs and the oracle FAILs: m1_stale_backfill: S7_LINEARIZABLE, S1_ACKED_DURABLE
```

A one-command demonstration runs the same comparison, replays the failure, and shows both safety gates
refusing to execute a candidate (about 30 s; it never executes candidate code):

```bash
bash scripts/run_demo.sh          # from the repository root; expect "checks passed: 9, failed: 0"
```

It needs `bash` (Git Bash on Windows), the virtual environment's `python` on `PATH`, a running PostgreSQL 16
server and `sha256sum` (not part of macOS; install GNU coreutils there). It writes only to a fresh directory
under `.gauntlet/` and exits with status 0 only if every check passed. The step-by-step walkthrough is
[DEMO.md](DEMO.md).

## 4. Commands

```bash
python -m gauntlet scenarios                         # list scenarios
python -m gauntlet impls                             # list references and mutants
python -m gauntlet run --scenario S02_backfill_vs_legacy_writes --impl m1_stale_backfill --seed 1
python -m gauntlet list                              # list persisted runs
python -m gauntlet show <run_id>                     # one persisted run
python -m gauntlet replay <run_id>                   # re-execute a persisted run, compare hashes
python -m gauntlet report <run_id>                   # Markdown report for one run
python -m gauntlet conventional --impl ref_a_trigger # conventional functional suite
# full benchmark (tens of minutes), into its own database and a new report file
GAUNTLET_RESULTS_DB="$PWD/.gauntlet/results-new.sqlite3" python -m gauntlet bench --seeds 1,2,3 --write-results docs/RESULTS-new.md
python -m gauntlet bench --impls ref_a_trigger,m1_stale_backfill --scenarios S02_backfill_vs_legacy_writes --seeds 1 --no-conventional
```

The analyses have their own commands: `diff`, `explore`, `minimize` and `experiment`
([ANALYSES.md](ANALYSES.md)). External candidates use `candidate validate | register | verify | list |
evaluate | isolation` ([CANDIDATES.md](CANDIDATES.md)). `python -m gauntlet <command> --help` lists every
option.

`docs/RESULTS.md` (v0.1.0), `docs/RESULTS-v0.2.0.md` and `docs/RESULTS-v0.3.0.md` are records of specific
benchmarks. Write new benchmarks to a new file and, ideally, a new results database. Never overwrite the
recorded ones.

Every `run` is persisted to the results database (by default `.gauntlet/results.sqlite3`) and is visible in
the dashboard.

## 5. Configuration

GAUNTLET is configured with environment variables. All of them are optional.

| Variable | Default | Purpose |
|---|---|---|
| `GAUNTLET_PG_DSN` | `postgresql://gauntlet:gauntlet@127.0.0.1:55432/postgres` | Administrator connection to the PostgreSQL 16 server. The default password is a published development default, not a secret; use it only for a server bound to `127.0.0.1` |
| `GAUNTLET_HOME` | `<repository>/.gauntlet` | Working directory: local PostgreSQL cluster, results database, candidate snapshots (git-ignored) |
| `GAUNTLET_RESULTS_DB` | `$GAUNTLET_HOME/results.sqlite3` | SQLite file for run records. Every command adds missing tables to the file it opens, so **never point it at a file in `evidence/`**; work on a copy |
| `GAUNTLET_CANDIDATES_DIR` | `$GAUNTLET_HOME/candidates` | Where registered candidate snapshots are stored |
| `GAUNTLET_PG_BIN` | portable binaries under `$GAUNTLET_HOME/pgdist` | An existing PostgreSQL 16 `bin/` directory for `gauntlet pg init/start/stop` |
| `GAUNTLET_STEP_TIMEOUT_S` | `20` | How long one granted statement may run without completing or blocking on a lock |
| `GAUNTLET_STEP_CANCEL_GRACE_S`, `GAUNTLET_STEP_STALL_GAP_S` | `5`, `2` | Evidence thresholds for attributing a step timeout to a candidate ([SECURITY_MODEL.md](SECURITY_MODEL.md)) |
| `GAUNTLET_LOCK_TIMEOUT_MS`, `GAUNTLET_MONITOR_LOCK_TIMEOUT_MS` | `15000` | Lock-wait bounds for actor and harness connections |
| `GAUNTLET_MAX_RESULT_ROWS`, `GAUNTLET_MAX_RESULT_BYTES` | `100000`, 16 MiB | Largest result one statement may return to an actor |
| `GAUNTLET_ACTOR_CONNECT_TIMEOUT_S` | `10` | Bound on opening an actor's database connection |
| `GAUNTLET_MAX_CANDIDATE_RUNS` | `2` | External-candidate runs that may execute at the same time |
| `GAUNTLET_API_ALLOWED_HOSTS`, `GAUNTLET_API_ALLOWED_ORIGINS` | none | Extra `Host` names and dashboard origins the API accepts besides the loopback defaults |

The timeouts are safety nets, not part of the logical schedule: a run that hits one is recorded as an
infrastructure error, a timeout or a candidate failure, never as a pass.

## 6. Dashboard

```bash
python -m gauntlet serve                 # API on http://127.0.0.1:8000
cd frontend && npm install && npm run dev  # UI on http://localhost:5173 (proxies /api to :8000)
```

Both servers listen on the loopback interface only and have no authentication; keep it that way (do not
pass `--host`). The development server sends no CORS headers and serves only the dashboard's own files,
so other web pages open in your browser cannot read files or API data through it
([SECURITY_MODEL.md](SECURITY_MODEL.md#the-api-and-dashboard)).

| View | What it shows |
|---|---|
| **Overview** | Purpose, benchmark version, counts, PostgreSQL status, latest benchmark metrics (shown as *Unavailable* if none has run), detection matrix, mutant validation table |
| **Run benchmark** | Pick scenario, implementation and seed; start a run; status exactly as reported by the backend; persisted run list with selection for comparison |
| **Run → Execution timeline** | Swimlane of every event (statements by class, invocations, responses, lost responses, lock waits, crashes, snapshots, stage markers, violation triggers), filterable event table, per-event JSON plus ledger entry |
| **Run → Invariant report** | All 12 invariants: pass / fail / inconclusive / n/a / infra error, readable evidence, jump to the triggering event |
| **Run → Client ledger / Oracle snapshots** | The raw inputs the oracle used |
| **Run → Research report** | Markdown report generated from the run record (downloadable) |
| **Compare runs** | Configuration, schedule / trace / outcome equality, first divergence, invariant differences, outcome and final-state differences |
| **Scenario library** | Scenarios (with a per-seed schedule preview), implementations and mutations, invariants, fault catalog |
| **Research report** | Report generated from the latest completed benchmark |
| **Differential / Exploration / Minimization** | Start and inspect the analyses; replay and minimize actions on run detail |
| **Experiments** | Experiment reports with *Verify evidence* (re-hashes the manifest) |
| **Candidates** | Registered external candidates, snapshot integrity and evaluation summaries |

The HTTP interface behind the dashboard is described in [API.md](API.md).
