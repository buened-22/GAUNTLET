#!/usr/bin/env python3
"""Compare a benchmark re-execution with frozen benchmark evidence, cell by cell.

    python scripts/compare_benchmark.py evidence/results-bench-eed0c47427.sqlite3 path/to/new-results.sqlite3

A cell is one (implementation, scenario, seed). For every cell of the frozen database the script compares
the verdict, the schedule, trace and outcome hashes and the status of each invariant with the first run of
the same cell in the new database (later rows of a cell are same-seed reproducibility re-runs and are
ignored). It exits with status 0 only if every frozen cell is present in the new database and identical.

Both databases are opened read-only, and the frozen one as immutable: nothing is written to either file.
Produce the new database with, for example:

    GAUNTLET_RESULTS_DB="$PWD/.gauntlet/rerun.sqlite3" python -m gauntlet bench --seeds 1,2,3 --no-repro --no-conventional

Only the standard library is used.
"""

from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path

FIELDS = ("verdict", "schedule_hash", "trace_hash", "outcome_hash")


def cells(path: Path, immutable: bool) -> dict:
    uri = path.resolve().as_uri() + "?mode=ro" + ("&immutable=1" if immutable else "")
    conn = sqlite3.connect(uri, uri=True)
    conn.row_factory = sqlite3.Row
    out: dict = {}
    try:
        for row in conn.execute("SELECT * FROM runs ORDER BY created_at, id"):
            key = (row["impl_id"], row["scenario_id"], row["seed"])
            if key in out:
                continue
            data = json.loads(row["data_json"]) if row["data_json"] else {}
            out[key] = {**{f: row[f] for f in FIELDS},
                        "invariants": [(i["id"], i["status"]) for i in data.get("invariants", [])],
                        "infra_error": data.get("infra_error")}
    finally:
        conn.close()
    return out


def main(argv: list[str]) -> int:
    if len(argv) != 3:
        print(__doc__.strip().splitlines()[2].strip(), file=sys.stderr)
        return 2
    frozen_path, new_path = Path(argv[1]), Path(argv[2])
    for p in (frozen_path, new_path):
        if not p.is_file():
            print(f"not a file: {p}", file=sys.stderr)
            return 2
    frozen, new = cells(frozen_path, immutable=True), cells(new_path, immutable=False)
    missing = sorted(set(frozen) - set(new))
    common = sorted(set(frozen) & set(new))
    print(f"frozen cells: {len(frozen)}   cells in the new database: {len(new)}   "
          f"missing from the new database: {len(missing)}")
    same = {f: 0 for f in (*FIELDS, "invariants")}
    identical = 0
    differences = []
    for key in common:
        a, b = frozen[key], new[key]
        differing = [f for f in same if a[f] != b[f]]
        for f in same:
            same[f] += f not in differing
        identical += not differing
        if differing:
            differences.append((key, differing, b["infra_error"]))
    for f, n in same.items():
        print(f"  {f:<14} identical in {n}/{len(common)} cells")
    verdicts: dict = {}
    for key in common:
        verdicts[new[key]["verdict"]] = verdicts.get(new[key]["verdict"], 0) + 1
    print(f"verdicts in the new database (compared cells): {dict(sorted(verdicts.items(), key=str))}")
    print(f"cells identical in verdict, hashes and invariant statuses: {identical}/{len(frozen)}")
    for key, differing, infra in differences[:20]:
        print(f"  DIFFERENT {key}: {', '.join(differing)}" + (f"   infrastructure error: {infra[:160]}" if infra else ""))
    for key in missing[:20]:
        print(f"  MISSING   {key}")
    return 0 if identical == len(frozen) and not missing else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv))
