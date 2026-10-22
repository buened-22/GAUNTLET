# Reproducibility

How another researcher can verify GAUNTLET's recorded evidence and reproduce its results from a clean
checkout. Every command here was run during the v0.4.0 release review (Windows 11, Git Bash, Python 3.14.0,
Node 24.11, PostgreSQL 16.14). Nothing here evaluates an AI coding agent.

## Reference environment

| Component | Recorded version | Required |
|---|---|---|
| OS | Windows 11 | Windows 11 verified; Linux/macOS expected to work for trusted code but **not verified** |
| Python | 3.14.0 | ≥ 3.11 |
| PostgreSQL | 16.14 (portable cluster installed by `gauntlet pg install`) | 16.x (the results record 16.14) |
| Node.js / npm | 24.11.0 / 11.6.1 | Node ≥ 18 (dashboard build, portable PostgreSQL download) |
| Python packages | exact versions in `backend/constraints.txt` | install with `-c backend/constraints.txt` for identical versions |

Each evaluation also records its own environment (harness source fingerprint, git revision and working-tree
state, PostgreSQL, Python and package versions), so results can be compared with the environment that
produced them.

## 1. Clean checkout and setup

```bash
git clone <repository-url> gauntlet && cd gauntlet
python -m venv .venv && source .venv/Scripts/activate          # macOS/Linux: source .venv/bin/activate
pip install -c backend/constraints.txt -e "backend[dev]"
python -m gauntlet pg install      # portable PostgreSQL 16.14 via npm into .gauntlet/pgdist (needs Node)
python -m gauntlet pg init         # .gauntlet/pgdata, user gauntlet / password gauntlet, port 55432
python -m gauntlet pg start
python -m gauntlet pg check        # "is_pg16": true
```

To use an existing PostgreSQL 16 server instead, set
`GAUNTLET_PG_DSN=postgresql://<user>:<password>@<host>:<port>/postgres` (the user needs `CREATEDB` and
`CREATEROLE`, because each run creates and drops its own database and, for candidates, a restricted role).

Nothing machine-specific is required: all state lives under the git-ignored `.gauntlet/` directory, and
no credentials other than the local development defaults are used.

## 2. Verify the frozen evidence (read-only)

```bash
(cd evidence && sha256sum -c *.sha256)  # four OK lines
```

The v0.3.0 git tag (commit `694577b709484b7ff274f049e470da5d9854ec45`) lives in the separate development
repository and is not part of this repository's history; see [HISTORY.md](HISTORY.md).

To inspect a frozen database without modifying it, copy it first. Every GAUNTLET command, including
`report` and `serve`, adds missing tables to the database it opens, so a used copy no longer matches the
checksum:

```bash
export GAUNTLET_RESULTS_DB="$PWD/.gauntlet/evidence-copy.sqlite3"
cp evidence/results-bench-eed0c47427.sqlite3 "$GAUNTLET_RESULTS_DB"
python -m gauntlet report --benchmark   # regenerates the v0.3.0 summary from the recorded runs
```

The v0.3.0 summary reconciles with its raw run rows: 180 distinct cells (6 scenarios × 10 implementations ×
seeds 1–3), 121 pass and 59 fail, 108 invariant-level FAIL entries in those 59 runs, 0 inconclusive, 0
infrastructure errors; the other 60 run rows are the reproducibility re-runs (60/60 identical). This
reconciliation was recomputed independently from the run rows during the release review.

## 3. Run the tests

```bash
cd backend
python -m pytest -q -m "not pg"   # unit tests, no database
python -m pytest -q               # everything; PostgreSQL tests need the server from step 1
cd ../frontend
npm ci && npm run typecheck && npm run build
```

Release-review result: see the README's *Tests* section for the exact totals and the date they were run.

## 4. Synthetic end-to-end demonstration

Follow [DEMO.md](DEMO.md) steps 3 to 8, with a scratch results database
(`GAUNTLET_RESULTS_DB`) and, for candidates, a scratch snapshot directory (`GAUNTLET_CANDIDATES_DIR`).
Expected results, all of which were reproduced during the release review:

| Step | Expected |
|---|---|
| `diff --impls ref_a_trigger,m1_stale_backfill --scenario S02_backfill_vs_legacy_writes --seed 1` | `CONFIRMED_VIOLATION`; m1 fails S1_ACKED_DURABLE and S7_LINEARIZABLE |
| `diff --impls ref_a_trigger,ref_b_dualwrite ...` | `AGREEMENT` |
| `explore --scenario F01_backfill_race_probe --impl m1_stale_backfill` | completed; 18 schedules, 13 pass, 5 fail, 1 failure class, replay reproduced |
| `minimize <m1 run> --invariant S1_ACKED_DURABLE` | 104 → 19 directives, 1-minimal w.r.t. unit deletion, replay signature match |
| `replay <m1 run>` | schedule, trace and outcome hashes and verdict identical |
| `candidate evaluate ... --isolation process` (no trust flag) | blocked, exit code 3 |
| `candidate evaluate ... --isolation strict` | blocked, exit code 3 |
| `candidate evaluate ... --isolation process --trusted-dev-env ...` (synthetic fixture) | completed; S01 agreement, S02 confirmed violation; evidence manifest 5/5 unchanged |
| `GET /api/experiments/<id>/verify` | `{"ok": true, "checked": 5, "missing": [], "modified": []}` |

Run ids differ between executions (they are fresh identifiers); schedule, trace and outcome hashes of the
same configuration do not.

## 5. Re-running the benchmark

```bash
GAUNTLET_RESULTS_DB="$PWD/.gauntlet/results-new.sqlite3" \
  python -m gauntlet bench --seeds 1,2,3 --write-results docs/RESULTS-new.md
```

This takes tens of minutes. Always write to a new database and a new results file; never overwrite
`docs/RESULTS*.md` or anything in `evidence/`. Compare the new summary with
`evidence/results-bench-eed0c47427.sqlite3` by verdicts and invariant statuses per cell and by schedule,
trace and outcome hashes. Identical hashes are expected on PostgreSQL 16.14; other PostgreSQL builds or
configurations may change traces (e.g. lock behaviour), which should be reported, not hidden.

## 6. Reporting a reproduction

Open an issue with: platform, Python, PostgreSQL and package versions (`pip freeze`), the output of the
evidence check, the test totals, the demo outcomes above, and any difference. A failed reproduction is as
valuable as a successful one.
