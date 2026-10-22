# Demo script (3 to 5 minutes)

A live walkthrough of the synthetic proof of concept. Prepare everything in *Before the session*; the
live part runs four commands. If anything fails live, switch to the captured output in
[demo-transcript.md](demo-transcript.md) and say so.

## Before the session (not shown)

```bash
source .venv/Scripts/activate                              # macOS/Linux: source .venv/bin/activate
python -m gauntlet pg start && python -m gauntlet pg check # "is_pg16": true
export GAUNTLET_RESULTS_DB="$PWD/.gauntlet/demo.sqlite3"
export GAUNTLET_CANDIDATES_DIR="$PWD/.gauntlet/demo-candidates"
cp evidence/results-bench-eed0c47427.sqlite3 "$GAUNTLET_RESULTS_DB"   # never use the evidence file itself
cd backend
# Optional, slow parts done in advance (about 2-4 minutes together):
python -m gauntlet explore --scenario F01_backfill_race_probe --impl m1_stale_backfill
```

Have [../README.md](../README.md) (the diagram) and this script open.

## 0:00 – 0:45 The problem (talk over the README diagram)

> "Online migrations run while old and new versions of an app share one database. Changing how money
> is stored has to stay exact to the cent while a legacy writer keeps writing, the backfill runs in
> batches, processes crash and clients retry. Ordinary tests run one client at a time with no crashes,
> so a backfill that writes a value it read a moment ago passes them. GAUNTLET gates every SQL statement
> of every process, so we can produce the bad interleaving on purpose, replay it exactly, and judge the
> result with an oracle that never trusts the implementation's own reports."

## 0:45 – 1:45 Differential testing

```bash
python -m gauntlet diff --impls ref_a_trigger,m1_stale_backfill --scenario S02_backfill_vs_legacy_writes --seed 1
```

Expected (about 15–30 s):

```text
comparison diff-…: CONFIRMED_VIOLATION  behaviour differs and the oracle FAILs: m1_stale_backfill: S7_LINEARIZABLE, S1_ACKED_DURABLE
  ref_a_trigger              run … pass           failed=[]
  m1_stale_backfill          run … fail           failed=['S7_LINEARIZABLE', 'S1_ACKED_DURABLE']
```

> "Same schedule, two implementations. The mutant's backfill copies a stale value; an acknowledged
> legacy write is lost. A difference alone wouldn't mean much, which is why this says *confirmed*: the
> oracle independently found an invariant violation." Note the mutant's run id.

Optional contrast: `--impls ref_a_trigger,ref_b_dualwrite` gives `AGREEMENT`: two different correct
designs whose internal bookkeeping differs. "Agreement isn't a proof of correctness, and we never claim it
is."

## 1:45 – 2:30 Exploration (show the pre-run result, or run it live if time allows)

Expected:

```text
exploration expl-…: completed (frontier exhausted); executed 18 {'pass': 13, 'fail': 5}; exhaustive within bounds: True; known unexplored: 0
  failure class c554f673482f: ['S1_ACKED_DURABLE', 'S7_LINEARIZABLE'] ['lost_ack'] x5 replay reproduced: True
```

> "Instead of hoping a random schedule hits the race, we enumerate every interleaving of this small
> fixture with at most one preemption: 18 schedules, 5 fail, all the same failure class, and the
> representative replays exactly. The claim is bounded: one directive, one preemption, no crash points."

## 2:30 – 3:30 Minimization and replay

```bash
python -m gauntlet minimize <mutant run id> --invariant S1_ACKED_DURABLE     # about 1-2 minutes
python -m gauntlet replay <mutant run id>
```

Expected:

```text
minimization mini-…: completed ; 1-minimal with respect to unit deletion
  reduction (original -> minimized): {"directives": [104, 19], ..., "trace_events": [390, 51], "units": [58, 3]}
  replay: {..., "outcome": "fail", "signature_match": true, ...}
schedule_hash  same  93b9e5c8f3edbed7  93b9e5c8f3edbed7
trace_hash     same  02019389e4b0f7c1  02019389e4b0f7c1
outcome_hash   same  74a63f00c8ed4b68  74a63f00c8ed4b68
```

> "The 104-step schedule shrinks to three operations: expand, a backfill batch paused before its write,
> and one legacy charge on order 4 in between. That's the bug, readable. Removing any one of those three
> no longer reproduces this failure, and the original run replays identically at the logical level."

If time is short, run only `replay` live and show the minimization lines from the transcript.

## 3:30 – 4:30 External candidates and the safety gate

```bash
python -m gauntlet candidate evaluate --id cand-6478453bb5dd4e78 --isolation process \
    --scenarios S01_baseline_migration --seeds 1 --refs ref_a_trigger
```

(Register first, before the session: `python -m gauntlet candidate register tests/candidates/synthetic_stale_backfill`.)

Expected: `BLOCKED: IsolationUnavailable: process isolation is not a security boundary …`, exit code 3.

> "This is the pipeline we'd use for agent-written code: content-hashed snapshots, the same analyses,
> reports with hashed evidence. It refuses to run without a declared trusted environment, and strict
> container isolation isn't implemented, so it fails closed. We have **not** evaluated any AI agent. The
> fixture we use is a hand-made copy of a reference with an injected defect."

Show `candidate-summary.md` from the pre-run trusted evaluation (or the transcript): S02 is a confirmed
violation and the experiment's evidence manifest verifies 5/5.

## 4:30 – 5:00 Close

> "What exists: a reproducible adversarial instrument with an independent oracle, validated on
> hand-written implementations, with bounded exploration and minimization. What doesn't yet: any agent
> evaluation, a real isolation boundary, and independent reproduction. Those are the next three steps."

## Claims to make, and not make

| Say | Do not say |
|---|---|
| "validated on hand-written references, mutants and synthetic fixtures" | "evaluated AI agents" / "agents produce these bugs at rate X" |
| "exhaustive within one directive and one preemption" | "exhaustive" / "proves correctness" |
| "confirmed invariant violation" | "root cause" (diagnoses mean *consistent with*) |
| "process mode is a development sandbox, not a security boundary" | "sandboxed" / "safe to run untrusted code" |
| "author-run, not independently reproduced" | "independently validated" |
