# Research design

Hypotheses, variables and threats to validity. For how experiments are executed and verified
(differential testing, exploration, minimization, replay, evidence integrity), see
[RESEARCH_METHODOLOGY.md](RESEARCH_METHODOLOGY.md).

## Research question

> Can AI coding agents produce software that passes conventional functional tests but violates safety or
> recovery guarantees under previously unseen combinations of concurrency, partial failure, retries and
> interrupted execution?

This prototype does **not** answer that question yet: no AI agent has been evaluated. What it builds
and validates is the *instrument* needed to answer it:

1. a task family with a precise correctness specification;
2. a controllable, reproducible adversarial execution engine;
3. an independent oracle;
4. evidence that the instrument separates correct from incorrect implementations for the intended
   reasons.

## Hypotheses

**H1 (instrument validity).** On this task, the oracle reports no violation for implementations
built to the protocol (the references), and reports the *intended* violation for implementations
containing known, realistic defects (the mutants).

* *Test.* Benchmark matrix and mutant validation (`python -m gauntlet bench`).
* *Status.* Measured. Current: `RESULTS-v0.3.0.md` (v0.3.0). Historical: `RESULTS-v0.2.0.md` (v0.2.0)
  and `RESULTS.md` (v0.1.0).

**H2 (gap between conventional and adversarial testing).** Some realistic defects in migration code
survive a reasonable conventional functional test suite but are exposed by adversarial schedules.

* *Test.* Run the frozen conventional suite (`conventional.py`) and the adversarial matrix on the
  same implementations.
* *Status.* Measured for hand-written mutants only. In the v0.3.0 benchmark, m2's conventional result
  is inconclusive: its CT03 ended in an infrastructure error of unknown cause. A later supplementary
  re-run, kept as a separate record, passed 10/10 but does not replace the recorded result (see
  `evidence/README-bench-eed0c47427.md`).
* *Caveat.* The conventional suite is one engineer's suite (see validity threats).

**H3 (target hypothesis, future work).** Solutions produced by AI coding agents for this task show
the H2 pattern at a meaningful rate.

* *Status.* **Not tested.** It requires collecting real agent solutions under equivalent
  conditions.

**H4 (reproducibility).** Given the same seed, configuration, PostgreSQL version and
implementation, a run reproduces the same logical schedule, the same statement-level trace and the
same outcome.

* *Test.* Re-execution and comparison of the three hashes.
* *Status.* Measured. Current: `RESULTS-v0.3.0.md` (v0.3.0). Historical: `RESULTS-v0.2.0.md` (v0.2.0)
  and `RESULTS.md` (v0.1.0).

## Variables

| Kind | Variable | Values in this prototype |
|---|---|---|
| Independent | Implementation | 2 references (strategies A, B) and 8 single-point mutants |
| Independent | Scenario | 6 (S01–S06), each a directive template whose fault opportunities are fixed |
| Independent | Seed | integers; controls the initial data and amounts in every scenario, plus operation targets and statement-level interleavings in every scenario since v0.2.0. Per scenario it additionally controls: S01 the backfill batch size; S03 the episode order, failing and retrying instances, and batch size; S04 the crash positions, which batches are interrupted, batch size and read order (S04's directive template, which is not seeded, repeats `expand` both before the backfill and, since v0.3.0, again after it); S05 the batch size and drain order. In v0.1.0 only S02 and S06 had seeded targets and interleavings; in S01, S03, S04 and S05 the seed changed only amounts. |
| Controlled | PostgreSQL version and configuration | 16.14, local cluster, `lock_timeout = 15s` per actor session |
| Controlled | Benchmark version | `0.3.0` (current; historical benchmarks `0.2.0` and `0.1.0`); part of the schedule hash, so results from different versions are not run-for-run comparable |
| Dependent | Invariant outcomes | pass / fail / inconclusive / not_applicable / infra_error, per invariant |
| Dependent | Run verdict | derived (see `INVARIANTS.md`) |
| Dependent | Conventional suite outcome | pass / fail / infra_error, per test; per implementation passed, failed (at least one test failed) or inconclusive (no test failed, at least one infra_error; since v0.3.0) |
| Observational | Wall-clock timing | recorded, excluded from every comparison |

## Baselines

* **Reference implementations.** These establish that a scenario is *satisfiable*: if a reference
  fails a scenario, the scenario or the oracle is wrong, not the mutant.
* **The conventional functional suite.** Ten sequential, single-client tests of the stated
  requirements, asserting on API responses. It stands in for "ordinary tests" and was written
  before any conventional results were looked at. It has **not** been tuned to kill or spare
  particular mutants.

## Evaluation methodology

Three result types are kept strictly separate:

1. **Conventional functional results.** Pass, fail or infrastructure error per test (`conventional.py`).
   An infrastructure error is neither a pass nor a failure.
2. **Adversarial execution results.** The event trace, the ledger outcomes, injected faults and
   recovery actions (the engine).
3. **Independent oracle results.** Per-invariant status with evidence (`oracle/`).

A **mutant is killed as intended** when its documented invariant FAILs in at least one run of one
of its documented scenarios, with no infrastructure error in that run. A run that fails only for
other reasons is reported separately (`runs_failing_without_intended_invariant`). Inconclusive and
infrastructure-error runs are never counted as detections or as passes.

### Reported metrics

All of these are computed by `bench.py` from persisted runs:

* scenarios executed;
* runs completed;
* verdict counts;
* invariant violations;
* faulty variants detected, and killed as intended;
* inconclusive runs;
* infrastructure failures;
* reproducibility (re-runs whose schedule, trace, outcome and verdict are all identical);
* conventional suite results per implementation.

No agent ranking is produced. Rankings require multiple real agent runs under equivalent
conditions.

## Threats to validity

### Internal

* *Oracle errors.* The oracle is new code. Mitigations:
  * unit tests on hand-built histories;
  * references must pass every scenario;
  * mutants must fail for their intended reason.

  One oracle defect was found and fixed during development. The first `lost_ack` relaxation could
  not explain a write that became visible and was then overwritten; a test exposed it.
* *Relaxation-based diagnosis is heuristic.* "Consistent with a lost write" is not a proof of root
  cause. Several relaxations could in principle explain the same history.
* *Permissive windows.* An uncertain operation's effect may be placed in any of its attempts'
  windows. That is slightly more permissive than reality, so it can only hide violations, never
  invent them.
* *Scenario authorship bias.* The same author wrote the scenarios, the references and the mutants.
  Scenarios may be (unconsciously) shaped around the mutants. The mitigation is that injection
  points are black-box statement-class conditions, and the random interleaving segments are not
  mutant-specific. Independent scenario authors would be stronger.

### Construct

* *Conventional suite representativeness.* One small suite is a weak proxy for "conventional
  testing". Several mutants that survive it (m3, m7, m8) *could* be caught by additional sequential
  tests. Only m1 and m2 strictly need interleaving or fault injection. All results files
  (`RESULTS-v0.3.0.md`, and the historical `RESULTS-v0.2.0.md` and `RESULTS.md`) should be read with
  this in mind.
* *Mutants versus agent bugs.* Hand-written mutants are plausible defects. They are not a sample
  of the defects AI agents actually make.

### External

* *One task family, one database, one machine.* Results may not transfer to other migration
  shapes, other isolation levels, other databases or distributed deployments.
* *Statement-level granularity.* Real systems interleave inside statements and across network
  partitions, which are not modelled.
* *No independent validation.* Every benchmark, reproducibility check, test run and the self-audit
  was run by the author. None has been reproduced by anyone else, on other machines or other
  PostgreSQL builds.

## Related work (to be reviewed before claiming novelty)

This prototype builds on well-established ideas and makes **no novelty claim**. A proper review
should cover at least:

* linearizability checking of recorded histories (Wing & Gong; Knossos and Porcupine in the Jepsen
  ecosystem);
* deterministic simulation testing of distributed systems;
* systematic concurrency testing and controlled scheduling;
* mutation testing and mutant equivalence;
* online schema-migration practice (expand/contract, trigger-based tools);
* benchmark suites for AI coding agents.

No comparison with existing tools has been run, so no claim of outperforming them is made.
