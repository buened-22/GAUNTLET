# Captured demo transcript (fallback)

Output of the documented demonstration ([docs/DEMO.md](../docs/DEMO.md) steps 4–8), captured on 2026-10-22 during
the v0.4.0 release review: Windows 11, Python 3.14.0, PostgreSQL 16.14, run from `backend/` against a scratch
copy of the v0.3.0 evidence database. Local paths are replaced by `<scratch>`; nothing else was edited.
Run ids are fresh identifiers and differ on every execution; hashes of the same configuration do not.
The candidate is the labelled synthetic fixture `synthetic_stale_backfill`, not AI-generated code.

```text
# step 4: differential

$ python -m gauntlet diff --impls ref_a_trigger,m1_stale_backfill --scenario S02_backfill_vs_legacy_writes --seed 1
comparison diff-9efec327b476: CONFIRMED_VIOLATION  behaviour differs and the oracle FAILs: m1_stale_backfill: S7_LINEARIZABLE, S1_ACKED_DURABLE
  ref_a_trigger              run bb5f960e37bd4a2a pass           failed=[]
  m1_stale_backfill          run f107318690f64b5a fail           failed=['S7_LINEARIZABLE', 'S1_ACKED_DURABLE']
[exit 0]

$ python -m gauntlet diff --impls ref_a_trigger,ref_b_dualwrite --scenario S02_backfill_vs_legacy_writes --seed 1
comparison diff-11f580dbfc9b: AGREEMENT  no normalized difference in client outcomes or final state
  ref_a_trigger              run f6fc7d9261804ece pass           failed=[]
  ref_b_dualwrite            run beb05c0b29fc43f6 pass           failed=[]
[exit 0]

# step 5: exploration

$ python -m gauntlet explore --scenario F01_backfill_race_probe --impl m1_stale_backfill
exploration expl-d3c3811e63ea: completed (frontier exhausted); executed 18 {'pass': 13, 'fail': 5}; exhaustive within bounds: True; known unexplored: 0
  failure class c554f673482f: ['S1_ACKED_DURABLE', 'S7_LINEARIZABLE'] ['lost_ack'] x5 replay reproduced: True
[exit 0]

# step 5: minimization and replay

$ python -m gauntlet minimize f107318690f64b5a --invariant S1_ACKED_DURABLE
minimization mini-cf65fd4e168f: completed ; 1-minimal with respect to unit deletion
  reduction (original -> minimized): {"directives": [104, 19], "operations_defined": [54, 3], "attempts_executed": [55, 3], "trace_events": [390, 51], "units": [58, 3]}
  replay: {"run_id": "bfa711b62f634faa", "outcome": "fail", "signature_match": true, "same_trace_as_accepted": true, "same_outcome_as_accepted": true}
[exit 0]

$ python -m gauntlet replay f107318690f64b5a
schedule_hash  same  93b9e5c8f3edbed7  93b9e5c8f3edbed7
trace_hash     same  02019389e4b0f7c1  02019389e4b0f7c1
outcome_hash   same  74a63f00c8ed4b68  74a63f00c8ed4b68
verdict        same  fail  fail
[exit 0]

# step 6: external candidate (synthetic fixture)

$ python -m gauntlet candidate isolation
{
 "platform": "win32",
 "strict": {
  "available": false,
  "reason": "no container runtime (docker/podman) found and container execution is not implemented in this version"
 },
 "process": {
  "available": true,
  "security_boundary": false,
  "process_tree_control": "windows-job-object",
  "resource_limits": "job object (memory, cpu time, process count)"
 }
}
[exit 0]

$ python -m gauntlet candidate validate tests/candidates/synthetic_stale_backfill
{
 "ok": true,
 "problems": [],
 "source_hash": "6478453bb5dd4e7880bb5146476facbe659e3bbdc8238c0010bc1ea8ff6fe862",
 "excluded": []
}
[exit 0]

$ python -m gauntlet candidate register tests/candidates/synthetic_stale_backfill
registered cand-6478453bb5dd4e78 (impl id ext:cand-6478453bb5dd4e78, source hash 6478453bb5dd4e7880bb5146476facbe659e3bbdc8238c0010bc1ea8ff6fe862)
[exit 0]

$ python -m gauntlet candidate evaluate --id cand-6478453bb5dd4e78 --isolation process --scenarios S01_baseline_migration --seeds 1 --refs ref_a_trigger
evaluation eval-25fe898c47ed BLOCKED: IsolationUnavailable: process isolation is not a security boundary; refusing to execute a candidate unless the operator declares this host a trusted, disposable development environment (CLI --trusted-dev-env, API/config isolation.trusted_environment = true). Never use it for untrusted code.
[exit 3]

$ python -m gauntlet candidate evaluate --id cand-6478453bb5dd4e78 --isolation strict --scenarios S01_baseline_migration --seeds 1 --refs ref_a_trigger
evaluation eval-b522e060fddf BLOCKED: IsolationUnavailable: strict isolation unavailable: no container runtime (docker/podman) found and container execution is not implemented in this version
[exit 3]

$ python -m gauntlet candidate evaluate --id cand-6478453bb5dd4e78 --isolation process --trusted-dev-env --scenarios S01_baseline_migration,S02_backfill_vs_legacy_writes --seeds 1 --refs ref_a_trigger --explore-max-schedules 18 --minimize-budget 20 --out <scratch>/candidate-summary.md
evaluation eval-73d037590e92: completed
# Candidate evaluation `eval-73d037590e92`

Status: **completed**. Generated from persisted GAUNTLET records; observations and interpretation are kept separate.

## Candidate

* Name `synthetic-stale-backfill`, candidate id `cand-6478453bb5dd4e78`, strategy A.
* Source hash (canonical, SHA-256): `6478453bb5dd4e7880bb5146476facbe659e3bbdc8238c0010bc1ea8ff6fe862`; contract 1; loader candidate-loader/1.
* Producer (as claimed in the manifest, not verified): `{'kind': 'synthetic_fixture', 'note': 'manually adapted; defect injected deliberately; not AI-generated'}`.
* Files (2): `candidate.py`, `gauntlet_candidate.json`

## Harness and task provenance

* Harness git revision `694577b709484b7ff274f049e470da5d9854ec45` (backend modified relative to it: True); harness source fingerprint `ba4eddceddbb0373` (identifies uncommitted changes; changed since the evaluating process started: False); oracle `92f780c37cb18a9d`; engine `fa6a7b9b973e5ca4`; PostgreSQL 16.14; Python 3.14.0; packages {'psycopg': '3.3.6', 'psycopg-binary': '3.3.6', 'fastapi': '0.143.3', 'pydantic': '2.14.1', 'uvicorn': '0.54.0'}.
* Agent task bundle (current hashes): `docs/agent_task/TASK.md` 37bba630eb5d6626, `backend/gauntlet/impls/base.py` ab2da06fd5c06a01, `backend/gauntlet/impls/legacy.py` 6e2cf37ad41f8df0, `backend/gauntlet/money.py` c607020c044c4598.
* Not recorded by GAUNTLET: the prompt actually given to an agent, the agent's tool and network settings, and the attempt number; these belong in the operator's study log (see docs/AGENT_EVALUATION_PROTOCOL.md).

## Isolation

* Mode `process`. process mode is a development sandbox, NOT a security boundary.
* Applied: allow-listed environment (no host secrets); per-actor temporary workspace; windows-job-object; job object (memory, cpu time, process count); audit-hook guard: no writes outside workspace, no process creation, no sockets, no ctypes; per-run PostgreSQL role (owner of its run database only; not superuser).
* Not enforced: OS-level read isolation (host files readable by the host user remain readable); OS-level network isolation (native code can open sockets); protection against native code defeating the audit-hook guard.
* Strict (container) isolation on this host: no container runtime (docker/podman) found and container execution is not implemented in this version.

## Benchmark configuration

* Benchmark version 0.3.0; scenarios ['S01_baseline_migration', 'S02_backfill_vs_legacy_writes']; seeds [1]; references ['ref_a_trigger']; per-run limit 180 s.
* Fair comparison: for each (scenario, seed) every implementation executed the identical schedule document on its own freshly created database with the same initial data; references used the harness role, the candidate a restricted per-run role and sandboxed actor processes (this difference in execution conditions is intentional).

## Observations

* Benchmark comparisons completed: 2 of 2.

| Scenario | Seed | Candidate outcome | Candidate FAILed invariants | References | Differential classification |
|---|---|---|---|---|---|
| S01_baseline_migration | 1 | pass | - | ref_a_trigger: pass | agreement |
| S02_backfill_vs_legacy_writes | 1 | fail | S7_LINEARIZABLE, S1_ACKED_DURABLE | ref_a_trigger: pass | confirmed_violation |

* Candidate runs that did not reach a verdict (infrastructure, timeout, candidate execution failure, inconclusive): 0.

### Exploration

* `expl-4c0992d8cbad` on F01_backfill_race_probe: 18 schedules, status completed; outcomes {'pass': 13, 'fail': 5}.
* Coverage: every schedule of directive 8 with at most 1 preemption(s) was executed; known unexplored schedules: 0. Bounds: at most 1 preemption(s), 18 schedules, one interleave directive; no crash-point exploration.
* Failure class `c554f673482f`: ['S1_ACKED_DURABLE', 'S7_LINEARIZABLE'] (['lost_ack']), 5 schedule(s); replay: {'run_id': '32760d2fb92948d7', 'same_trace': True, 'same_outcome': True, 'same_choices': True, 'verdict': 'fail', 'failure_class': 'c554f673482f', 'reproduced': True}.

### Minimization

* Candidate failing runs: 1; minimized: 1.
* `mini-3518141ec9a7` (S02_backfill_vs_legacy_writes, S7_LINEARIZABLE): completed; 1-minimal with respect to unit deletion; reduction {'directives': [104, 19], 'operations_defined': [54, 3], 'attempts_executed': [55, 3], 'trace_events': [390, 51], 'units': [58, 3]}; independent replay {'run_id': 'efc2aca324384924', 'outcome': 'fail', 'signature_match': True, 'same_trace_as_accepted': True, 'same_outcome_as_accepted': True}; original unchanged True.

### Report and evidence

* Experiment report `expe-58d09c941664` (markdown SHA-256 `9fb9f86e3e0729fd`); completeness complete; evidence-manifest verification: all 5 artifacts present and unchanged.
* Paired with `ref_a_trigger` (matched scenario and seed): both pass 1, only reference fails 0, only candidate fails 1, both fail 0, unmatched / non-conclusive 0.

## Interpretation (not observations)

* A FAIL is a confirmed invariant violation observed under these schedules; the oracle's diagnosis states what the evidence is consistent with, not a proven root cause.
* Passing every scenario is evidence about these schedules only; it does not show the candidate is correct.
* One candidate is one sample: nothing here estimates how often an agent or model produces correct migrations.

## Reproduction

1. Verify the snapshot: `python -m gauntlet candidate verify cand-6478453bb5dd4e78` (hash `6478453bb5dd4e78…`).
2. Re-run: `python -m gauntlet candidate evaluate --id cand-6478453bb5dd4e78 --scenarios S01_baseline_migration,S02_backfill_vs_legacy_writes --seeds 1`.
3. Replay any run: `python -m gauntlet replay <run_id>` (logical hashes, not timing, are compared).

wrote <scratch>/candidate-summary.md
[exit 0]

$ python -m gauntlet candidate verify cand-6478453bb5dd4e78
ok: cand-6478453bb5dd4e78 matches its recorded source hash 6478453bb5dd4e7880bb5146476facbe659e3bbdc8238c0010bc1ea8ff6fe862
[exit 0]

# step 8: evidence verification through the API
$ curl http://127.0.0.1:8000/api/experiments/expe-58d09c941664/verify
{"ok":true,"checked":5,"missing":[],"modified":[]}
```
