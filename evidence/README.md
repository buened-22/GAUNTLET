# Frozen benchmark evidence

`results-bench-f5316f9d4b.sqlite3` is a read-only snapshot of the results database (`.gauntlet/results.sqlite3`).
It was taken on 2026-10-18 with SQLite's online backup API, before the repository was initialised. It is the raw
evidence behind `docs/RESULTS.md` and every run id cited in `docs/GAUNTLET_VALIDATION_AUDIT.md`.

The live database under `.gauntlet/` is not tracked. It changes every time a run is started from the CLI or the
dashboard. Do not overwrite this file. Store new results under a new name.

## Contents (verified when the snapshot was taken)

| Item | Value |
|---|---|
| Benchmark | `bench-f5316f9d4b`, status `completed` (benchmark version 0.1.0, PostgreSQL 16.14) |
| Rows tagged with this benchmark | 240: 180 matrix runs (10 implementations × 6 scenarios × seeds 1–3) and 60 reproducibility re-runs |
| Rows not tagged with the benchmark | 3: two S03 seed-11 demonstration runs (`ref_b_dualwrite`, `m2_nonatomic_dual_write`), and one replay of run `165ca695d02c49e5` made during the validation audit |
| `PRAGMA integrity_check` | `ok` |
| SHA-256 | see `results-bench-f5316f9d4b.sqlite3.sha256` |

## Verify

```bash
cd evidence
sha256sum -c results-bench-f5316f9d4b.sqlite3.sha256
```

## Inspect

The CLI reads the database named by the `GAUNTLET_RESULTS_DB` environment variable. To regenerate the
benchmark report from the frozen copy, set it to an absolute path to this file:

```bash
# from backend/
GAUNTLET_RESULTS_DB="$PWD/../evidence/results-bench-f5316f9d4b.sqlite3" python -m gauntlet report --benchmark
```

Commands that write (`run`, `bench`, `replay`, `serve`) would change this file. Point them at a copy instead.
