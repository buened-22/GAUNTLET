# Audit scripts: provenance

These four scripts produced the evidence in `docs/GAUNTLET_VALIDATION_AUDIT.md`. They are byte-identical copies
of the files written and run during that audit, on 2026-10-18. The audit ran them from the Claude Code
session's temporary scratchpad, outside the repository:

```
C:\Users\<user>\AppData\Local\Temp\claude\<session-scratchpad>\
```

(The username and session id are redacted.) The copies were made with `cp -p`, checked with `cmp` against the
originals, and are not modified. They are not part of the application, and the test suite does not import them.

| File | SHA-256 | Purpose (audit section) |
|---|---|---|
| `audit_traces.py` | `341c37cb72e0c20004efd11db8e0a91ec5507a4e1006191329df2cc22b6d2f4c` | §4: print the trace excerpts that expose m1, m2, m3, m7, m8 from a results database |
| `audit_sequential.py` | `de354bb1a8cbab10d5973b3533ca30c1be2063c30ff2977ce32a059db6a19022` | §4: sequential, fault-free schedules against each mutant and its reference (also the first observation of Finding 1) |
| `audit_sequential_retry.py` | `d6aa39ee240e6f117231db83c8e06cf7fc3de52c539b2966a94b4ca4e8d29270` | §4: sequential same-logical-operation replays for m2 and m3 |
| `audit_oracle.py` | `3e59ee4a24ad4d6b14e82b7c9f009819a86ec4f18859e6281129c696a42f9aa8` | §5: 20 hand-constructed histories run through the oracle |

## Running them

Run them with the project virtualenv and the backend installed (`pip install -e "backend[dev]"`):

```bash
cd audit
python audit_oracle.py                        # no database needed
python audit_sequential.py                    # needs PostgreSQL 16; creates and drops its own databases
python audit_sequential_retry.py              # imports audit_sequential, so run it from this folder
python audit_traces.py ../evidence/results-bench-f5316f9d4b.sqlite3   # read-only queries
```

`docs/GAUNTLET_VALIDATION_AUDIT.md` still describes running these scripts from the scratchpad location. The
paths in that document were redacted, and the scripts now live here.
