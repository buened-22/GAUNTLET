# Open questions and next milestones

Ordered by how much each would change what GAUNTLET's results can be trusted to mean. None of these has
been started unless stated.

## 1. Independent reproduction

*Question.* Do the frozen results and the demo reproduce on another machine, operating system and
PostgreSQL build, run by someone other than the author?

*Milestone.* At least one external researcher follows [../docs/REPRODUCIBILITY.md](../docs/REPRODUCIBILITY.md)
and reports evidence checksums, test totals, demo outcomes and benchmark hashes. Linux would also verify
the untested POSIX code paths (for trusted code only).

## 2. Safe agent evaluation

*Question.* Do AI coding agents, given the task bundle in `docs/agent_task/`, produce migrations that pass
the conventional suite but violate invariants under adversarial schedules (hypothesis H3)?

*Prerequisites.* A verified isolation boundary meeting the requirements in
[../docs/SECURITY_MODEL.md](../docs/SECURITY_MODEL.md) (no host-file or oracle access, no network except its
own database, enforced resource limits, disposable environment), hostile-fixture tests with positive
controls, and an independent review of the boundary. Then: the pre-registered protocol in
[../docs/AGENT_EVALUATION_PROTOCOL.md](../docs/AGENT_EVALUATION_PROTOCOL.md), several attempts per agent
under equivalent conditions, and reporting that separates confirmed violations, inconclusive runs and
infrastructure errors.

## 3. Stronger oracle validation

*Questions.* Can the oracle accept an incorrect history (false pass) or reject a correct one (false
fail)? How much do the permissive uncertain-operation windows hide? Would an independently written
oracle agree?

*Milestones.* An independent second implementation of the S7 linearizability check run over all recorded
histories; per-attempt (rather than union) windows; witness linearizations recorded for passing runs;
oracle mutation testing (deliberately broken oracle rules must change benchmark verdicts).

## 4. Reducing authorship bias

*Question.* Are the scenarios shaped around the mutants the same author wrote?

*Milestones.* Mutants and scenarios authored independently of the oracle author; mutant-equivalence
analysis; a larger, independently written conventional suite as the baseline (m3, m7 and m8 might be
caught by better sequential tests, which would sharpen hypothesis H2).

## 5. Broader coverage

*Questions.* How does detection depend on exploration budget? What escapes the current bounds?

*Milestones.* Exploration over several directives and crash points; partial-order reduction or
coverage-guided search over seeds; detection reported as a function of budget; isolation levels other
than READ COMMITTED; network-level faults; other task families with model-based oracles (outbox
consumers, idempotent payment APIs, sharded counters).

## 6. Comparison with existing approaches

*Question.* What does GAUNTLET find that established approaches (linearizability checkers over recorded
histories, deterministic simulation testing, systematic concurrency testing, mutation testing,
schema-migration tooling) would or would not find on the same task?

No comparison has been run and no novelty is claimed. A literature review is required first; this
repository deliberately contains no bibliography, because the references have not been verified. Topics to
cover are listed in [../docs/RESEARCH_DESIGN.md](../docs/RESEARCH_DESIGN.md#related-work-to-be-reviewed-before-claiming-novelty).

## 7. Unexplained observations

* The v0.3.0 infrastructure error in conventional test CT03 for m2 (a statement that neither completed nor
  waited on a lock until the 20 s step timeout). Its cause is unknown. The supplementary conventional re-run (2026-10-19) had no infrastructure error, which
  neither explains nor rules out the original event.
