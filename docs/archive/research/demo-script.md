# Demo script: recorded video (about 5 minutes, six clips)

A recording plan for the current build (branch `gauntlet/full-audit-hardening`). Every command and output
below is real: the host part was rehearsed (see [demo-readiness-report.md](demo-readiness-report.md)) and the
Gemini evaluation was run once inside a disconnected VM ([gemini-attempt2-results.md](gemini-attempt2-results.md)).
Run ids (`diff-…`, `run …`, `eval-…`) are fresh on every execution; classifications, counts and logical hashes
are not. Security wording for questions: [../docs/DEMO_SECURITY_NOTES.md](../docs/DEMO_SECURITY_NOTES.md).

Record each clip separately and edit them together. Speed up waiting (for example 2–4×) and say so on screen.

## Before recording

Windows host, Git Bash, from the repository root (database started as in [../docs/DEMO.md](../docs/DEMO.md)):

```bash
source .venv/Scripts/activate                                        # macOS/Linux: source .venv/bin/activate
export GAUNTLET_PG_DSN=postgresql://gauntlet:gauntlet@127.0.0.1:55434/postgres   # or your own DSN
python -m gauntlet pg check                                          # "is_pg16": true
```

Do not re-run the Gemini evaluation in the VM for the video: it would overwrite `~/gemini-results/`, whose
hashes are recorded. Show the recorded outputs instead (clips 3 and 4). If you want a live re-run, first copy
the results (`cp -r ~/gemini-results ~/gemini-results-run1`) and keep the VM disconnected.

## Clip 1 (0:00 – 0:40) The problem

Show: [../README.md](../README.md) (the diagram) or the title of the replay page.

> "Online migrations run while old and new versions of an application share one database. Changing how money is
> stored has to stay exact to the cent while a legacy writer keeps writing, the backfill runs in batches,
> processes crash and clients retry. Ordinary tests run one client at a time, so they miss these failures.
> GAUNTLET controls every SQL statement of every process, schedules them adversarially, injects crashes and
> lost responses, and judges each run with an independent oracle that reads only what clients were told and
> the raw database. Every schedule is hashed and replays exactly."

## Clip 2 (0:40 – 1:50) The pipeline on the host, in one command (25 s run)

```bash
bash scripts/run_demo.sh <folder with the Gemini attempt-2 candidate.py and gauntlet_candidate.json>
```

Expected: `checks passed: 9, failed: 0`. Narrate the five sections as they appear:

> "One: the database is up and the frozen benchmark evidence matches its checksums. Two: a correct reference
> migration and a deliberately faulty one face the same adversarial schedule; the oracle passes the reference
> and catches the faulty backfill losing an acknowledged write. Three: that failure replays with identical
> hashes. Four: Gemini's candidate is checked against the contract, stored as a content-addressed read-only
> snapshot and verified, without running its code. Five: on this machine GAUNTLET refuses to run it, because
> process isolation is not a security boundary and VM isolation is not built in."

## Clip 3 (1:50 – 2:40) Running Gemini's code safely

Show, in this order:

1. VirtualBox settings of the VM: **Network: Disabled**, **Shared Folders** empty.
2. In the VM terminal:
   `clear; grep -E "isolation|ok: no shared|passed|failed|pipeline tests exit|^ok: cand|evaluate exit" ~/gemini-results/step3.log`,
   which shows the isolation check (`ok: no shared folder mounted, no network reachable`), the Linux pipeline
   tests (`73 passed, 3 skipped`), the snapshot verification and `evaluate exit code: 0`.

> "So I ran it where it can't hurt anything: a VM with no network and no shared folders. The script refuses to
> start otherwise. Before any of Gemini's code ran, GAUNTLET's candidate pipeline passed its own tests on
> Linux. Then the snapshot was verified against its hash and evaluated against six adversarial scenarios."

## Clip 4 (2:40 – 3:50) What GAUNTLET found

In the VM terminal: `clear; sed -n 25,60p ~/gemini-results/gemini-attempt2-summary.md`

> "Five of six scenarios pass, in agreement with the reference, including the backfill race and the crash
> scenarios; bounded exploration of the backfill race found no failing interleaving in 15 schedules. One
> fails: in premature finalization, the migration is asked to finalize and contract while old application
> instances are still running. The reference refuses until they drain. Gemini's migration goes ahead, and a
> request from the old version then fails: a compatibility violation. GAUNTLET minimized the 48-step schedule
> to two operations and replayed it with the same trace and outcome."

## Clip 5 (3:50 – 4:25) The code behind it

Show `candidate.py` in an editor: `migrator_finalize` and `migrator_contract`.

> "Neither step checks whether old instances are still registered before tightening or dropping the schema.
> The oracle didn't need to read the code to find this; the code just confirms what the oracle reported."

## Clip 6 (4:25 – 5:00) Scope and next step

> "What this is: one Gemini attempt, one seed: a demonstration of the pipeline on real AI-generated code, not
> a statement about Gemini in general. My original hypothesis was lost acknowledged writes; this attempt
> didn't show that, it showed a different real defect. Next: many independent attempts under a fixed protocol,
> and an isolation boundary built into GAUNTLET itself."

## If asked

* Full test suite: 510 passed, 1 failed, 0 skipped at `2a73551` (a known wall-clock threshold under disk load,
  FR-9); frozen benchmark 180/180 cells identical; evidence checksums 4/4.
* Audit fixes on this branch (A-01 harness-owned connections, FR-3 evidence-based timeout classification, A-02
  non-superuser observer), each with tests; see [../docs/DEMO_SECURITY_NOTES.md](../docs/DEMO_SECURITY_NOTES.md).
* Conventional tests: whether the Gemini candidate passes GAUNTLET's conventional suite was not measured; that
  suite does not run external candidates in this version.

## Claims to make, and not make

| Say | Do not say |
|---|---|
| "one Gemini-generated candidate, evaluated once, in a disconnected VM" | "Gemini produces this defect" / "evaluated Gemini" / any rate |
| "a confirmed invariant violation, minimized and replayed" | "root cause" (diagnoses mean *consistent with*) |
| "no lost acknowledged write was found in this attempt" | "Gemini's migration is safe under concurrency" |
| "exhaustive within one directive and one preemption" | "exhaustive" / "proves correctness" |
| "process mode is a development sandbox; the VM was the boundary" | "GAUNTLET sandboxes untrusted code" / "secure" |
| "author-run, not independently reproduced" | "independently validated" |
| "510 of 511 tests passed; the failure is a known timing threshold" | "all tests pass" |
