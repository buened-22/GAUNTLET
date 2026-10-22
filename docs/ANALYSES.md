# Analyses: differential testing, schedule exploration, failure minimization, experiment reports

These four capabilities were added after release v0.3.0. They reuse the existing runner, schedule
language, oracle and results store; they do not change the benchmark, its scenarios, its oracle or
any recorded result. Nothing here evaluates an AI coding agent: every implementation is a hand-written
reference or mutant.

All four build on one shared primitive: **executing an explicit schedule document** (`initial_orders`,
`directives`, `ops`) through `runner.execute_run(..., schedule=...)`. Catalog runs are unchanged (same
schedule documents, same hashes). Every execution creates its own PostgreSQL database, records
provenance (schedule id, initial-state fingerprint, implementation source hash, oracle source hash,
engine source hash, git revision, run database name, origin) and is persisted in the `runs` table, so
run detail, compare and replay work on it. Analyses are persisted in the `analyses` table, created on
demand (older result databases stay readable).

Run outcomes are `pass`, `fail`, `inconclusive`, or an infrastructure error with a class: `timeout`
(per-run wall-clock limit, enforced at every engine step and by a watchdog that kills the run's actor
processes), `limit_exceeded` (event limit), `schedule_error` (a schedule that cannot be executed as
written), `infrastructure`, `harness_exception`. None of these is ever reported as a pass.

Bounds: the per-run limit is checked at every engine step, and a watchdog thread kills the run's own actor
processes and cancels its monitor query when the limit passes (a worker that ignores the controller is
stopped; measured: 3.7 s for a 3 s limit, against 32 s without the watchdog). The harness's own DDL (create,
initialise and drop of the run database) runs with a 120 s server-side statement timeout. Single runs and
replays started through the API have a 600 s limit; `gauntlet bench` and `gauntlet run` are unchanged (no
run limit), so the v0.3.0 benchmark protocol is identical.

## Differential testing (`gauntlet.differential`)

Two or more implementations execute the same schedule document, each on a fresh database.
Client-visible behaviour is normalized and compared pairwise:

| Rule | What it does | Why |
|---|---|---|
| N1 | excludes `t_ms`, `backend_pid`, `blockers` | timing and OS ids carry no logical meaning |
| N2 | compares errors by code, not message text | messages are implementation prose |
| N3 | compares migration-operation results by status and error code only | payloads (rows per batch, rows reconciled) are unspecified bookkeeping |
| N4 | compares client operations on status, full result and error code | these are the acknowledgements and values clients see |
| N5 | compares the final snapshot's columns, NOT NULL flags, triggers, reported migration stage and every row exactly | final state, constraints and migration state are part of the contract |
| N6 | matches attempts by attempt id; a missing or extra attempt is a difference | retries are behaviour |

Unit tests show that missing records, wrong amounts, changed acknowledgements, different read
results, failed migration steps, missing or extra attempts and schema differences all survive
normalization. Rule N3 is what makes the two references agree on S02: their only difference is the
migration bookkeeping payload.

Classification: `inconclusive` (any execution error, or a difference with only inconclusive oracle
results), `confirmed_violation` (difference + an invariant FAIL in some run), `unconfirmed_disagreement`
(difference, every run passes the oracle), `agreement` (no difference; does not prove correctness; a
violation shared by all runs is flagged). The earliest meaningful divergence is the first differing
attempt with the preceding logical events of both traces; preceding events are context, not causes.
The oracle judges each run on its own (its result for a run is identical whether or not it is paired).

## Systematic schedule exploration (`gauntlet.explore`)

The explored object is one `interleave` directive (the one marked `"explore": true`, else the first).
At each step one *runnable* actor takes its next step: an invocation, one statement grant, or a
response delivery. Ordering constraints are structural: each actor's operations and statements stay
in program order, invocation precedes statements precedes response, and a statement waiting on a lock
is not runnable until the lock is released.

Search: stateless depth-first search with re-execution. The root is the deterministic non-preemptive
schedule; each executed schedule's decision log yields children `realized[:i] + [alt]` for every step
past its forced prefix and every other runnable actor, continued with the non-preemptive default.
Children whose preemption count exceeds `max_preemptions` are not generated (the default never
preempts, so the count is exact). No partial-order reduction is applied.

Claim: if the frontier empties with no errored executions, no non-replayable prefix, no duplicate
schedule (a sign of nondeterminism; duplicates are never expanded again) and no branch point beyond
`max_depth`, the status is `completed` and every schedule of that directive with at most
`max_preemptions` preemptions was executed (assuming deterministic execution; each failure class
representative is replayed and compared). An empty frontier with any of those gaps is `incomplete`. A
search stopped by `max_schedules` / `max_duration_s` is `stopped`, by cancellation `interrupted`, by an
exception `error`; each reports how many generated schedules were not executed (a lower bound on what
remains). No replay is executed after a cancellation or once the duration budget is spent; a replay that
itself ends in an execution error is reported as inconclusive, not as flaky.

Limits: `max_schedules`, `max_preemptions`, `max_depth`, `max_events_per_run`, `run_time_limit_s`,
`max_duration_s`, `max_retries` (re-execution after an infrastructure error).

Failure classes group failing schedules by the set of FAILed invariants and the oracle's diagnosis
categories. This is a symptom grouping, not a root-cause grouping.

Fixture `F01_backfill_race_probe` (not in the benchmark catalog): one backfill batch of four rows
against one legacy `set_total` on order 3.

## Failure minimization (`gauntlet.minimize`)

Delta debugging (complement removal, ddmin) over removable units: a logical operation's directive
group, an operation queued in an interleave, a snapshot, a `backfill_until_done`, a `repeat`
(dependent on its operation). `spawn`, `drain`, `crash`, `restart` and `stage` are kept. Candidates are
validated statically before they run; for an interleave with explicit choices, the choices of removed
operations are dropped and the remaining ones are followed leniently, and the accepted schedule is then
stored with the exact choices it took (strict replay).

Failure signature for target invariant T: T FAILs; the diagnosis categories are a non-empty subset of
the original's; and, for every identifying feature the original evidence carries, the candidate's
evidence overlaps it: affected orders (`order_id`, or the ids of `rows` in snapshot evidence), failing
operations (`op`) and error codes (policy `intersect`; `any` disables these overlap checks). Execution
errors, timeouts and other violated invariants never count as reproducing. `max_attempts` counts
candidate executions (including confirmation repeats), checked before every execution. A cancelled
minimization executes nothing further and reports its best accepted candidate as not replayed. The original is executed
`reproduce_repeats` times first; if it does not reproduce every time the result is `flaky` or
`not_reproducible` and nothing is minimized. The final schedule is replayed independently. The
original run record is hashed before and after (`original_unchanged`).

The result is *1-minimal with respect to unit deletion* only when the last pass rejected every
single-unit removal **on its evidence**: a removal whose execution ended in an infrastructure error or
timeout, or that the budget cut off, was never judged, so it voids the claim (the result then lists those
units in `one_minimal_unproven_units` and says it is not proven minimal). Otherwise, too, it says it is
not proven minimal. Batch-size or row-count
simplification is not attempted.

## Experiment reports (`gauntlet.experiment`)

An experiment is a versioned configuration over persisted records (a benchmark, run ids, analysis
ids) with a research question, hypotheses, exclusions, an optional stopping rule and a sampling
declaration. Revisions add versions; earlier versions are kept, and a preregistered or already
reported plan can only change with a recorded deviation reason. A confirmatory experiment must be
preregistered.

Metrics (each lists numerator and denominator run ids):

| Id | Metric | Unit | Denominator |
|---|---|---|---|
| M1 | run outcomes | run | all included runs |
| M2 | invariant-violation rate | run | conclusive (pass/fail) runs |
| M3 | share of runs with a confirmed violation | run | all included runs |
| M4 | detection rate on known-faulty implementations | mutant | mutants with a conclusive run |
| M5 | killed by the intended invariant | mutant | mutants with a conclusive run in an intended scenario |
| M6 | false-positive rate on references | run | conclusive reference runs |
| M7 | conventional test pass rate | test execution | tests that passed or failed (infra errors excluded and listed) |
| M8 | reproducibility | re-executed run | benchmark reproducibility re-runs |
| M9 | exploration counts | exploration | — |
| M10 | runtime (observational) | run | — |
| M11 | cost | — | unavailable (not recorded) |

Uncertainty: intervals (Wilson, 95%) are computed only when the configuration declares an independent
random sample; for the fixed, purposively constructed sets in this prototype the report states that no
interval is estimated and why. Small denominators are flagged. Paired comparisons count matched
(scenario, seed) cells descriptively; no significance test is run.

Completeness follows the stopping rule (benchmark matrix, or `required` impls × scenarios × seeds): a
cell is done only with a conclusive run; missing and unresolved cells are listed. Without a stopping rule
completeness is `not_assessed`.

The manifest lists every selected run with the SHA-256 of its stored record and the metrics it
supports, plus the benchmark summary and analyses hashes and the code fingerprints. Reports contain no
timestamp, so the same data yields byte-identical markdown. Reports and export bundles are redacted
(database credentials, key/token/secret/password values, `sk-` keys, user home paths).

The latest report's manifest is stored with the experiment; `GET /api/experiments/{id}/verify` (and every
regeneration) re-hashes the named artifacts and lists missing or modified ones. Runs a benchmark recorded
but the store no longer holds are counted as `missing` (and make the experiment incomplete). Explored
schedules are reported only by M9, never as independent runs in M1-M8. A selection that mixes benchmark
versions is refused unless `allow_mixed_versions` is set, and then carries a warning.

## Interfaces

CLI: `python -m gauntlet diff|explore|minimize|experiment ...` (see `--help`).
API: `/api/differential`, `/api/explorations`, `/api/minimizations`, `/api/runs/{id}/replay`,
`/api/replays/{id}`, `/api/experiments` (`/report`, `/report.md`, `/export`, `/verify`), `/api/analyses`,
`/api/fixtures`. `POST /api/analyses/{id}/cancel` applies to explorations and minimizations only (the
jobs that check for cancellation); other kinds return 409.
Dashboard: Differential, Exploration, Minimization and Experiments pages; Replay and Minimize actions on
run detail.

## Limitations

* One explored interleave directive per exploration; crash points are not explored (they are fixed by
  the schedule's `run_until` directives).
* No partial-order reduction; the preemption bound, not independence, limits the search.
* Exploration completeness assumes deterministic execution; replays check it but cannot prove it.
* Minimization removes whole units only.
* All validation is author-run on one machine. Physical timing differs between executions and is never
  compared.
