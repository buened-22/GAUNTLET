# Frozen benchmark evidence: `bench-7c4395ec2b` (benchmark version 0.2.0)

`results-bench-7c4395ec2b.sqlite3` is a read-only snapshot of `.gauntlet/results-v0.2.0.sqlite3`. That is
the separate results database the v0.2.0 benchmark was written to, so it is not the historical
`.gauntlet/results.sqlite3`. The snapshot was taken on 2026-10-18 with SQLite's online backup API. It is the
raw evidence behind `docs/RESULTS-v0.2.0.md`. Do not overwrite it.

## Contents (verified when the snapshot was taken)

| Item | Value |
|---|---|
| Benchmark | `bench-7c4395ec2b`, status `completed`, benchmark version 0.2.0, PostgreSQL 16.14 |
| Rows | 240, all tagged with this benchmark: 180 matrix runs (10 implementations × 6 scenarios × seeds 1–3) and 60 reproducibility re-runs (one per implementation × scenario, seed 1) |
| Other rows | none |
| `PRAGMA integrity_check` | `ok` |
| SHA-256 | `5834c1c39b930ec1312f9573b660c1e794ea13a462d6c4bcc1e6d87758e292ab` (see `results-bench-7c4395ec2b.sqlite3.sha256`) |

## Relationship to the historical v0.1.0 evidence

`results-bench-f5316f9d4b.sqlite3` (see `README.md`) is the historical v0.1.0 benchmark and stays
unchanged. The two benchmarks are not run-for-run comparable, because version 0.2.0 changed two things
after the validation audit (`docs/GAUNTLET_VALIDATION_AUDIT.md`):

1. **Oracle operation identity (audit Finding 1).** A logical write is identified by its idempotency key,
   not by the harness's internal operation id. Re-evaluating all 243 historical runs with the 0.2.0 oracle
   gives identical results, because every historical scenario derived keys from internal ids.
2. **Seed-driven schedule diversity (audit Finding 2).** In S01, S03, S04 and S05 the seed now changes
   target orders, episode order, failing and retrying instances, crash positions, batch sizes and
   statement-level interleavings, not only amounts. Under version 0.1.0, the same seed number produced a
   different schedule in those four scenarios. The benchmark version is part of every schedule hash.

## Verify

```bash
cd evidence
sha256sum -c results-bench-7c4395ec2b.sqlite3.sha256
```

On Windows without `sha256sum`, use `certutil -hashfile results-bench-7c4395ec2b.sqlite3 SHA256` and compare
the result with the `.sha256` file.

## Inspect

Run from `backend/`. `report` only reads, and was checked to leave the file's bytes unchanged:

```bash
GAUNTLET_RESULTS_DB="$PWD/../evidence/results-bench-7c4395ec2b.sqlite3" python -m gauntlet report --benchmark
```

Commands that write (`run`, `bench`, `replay`, `serve`) change the database they are given. Point them at
a copy.
