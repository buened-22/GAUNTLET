# Limitations and future work

## What the prototype does not prove

* **Nothing about AI agents in general.** No study of Gemini, Claude or any other AI coding agent has been
  run. One candidate generated with Gemini (attempt 2) was evaluated once, inside a disconnected VM, as a
  demonstration ([../research/gemini-attempt2-results.md](../research/gemini-attempt2-results.md)); one attempt
  says nothing about how often a model produces a defect. Every other implementation evaluated here was written
  by hand, as references or mutants, or is a labelled synthetic candidate fixture (a manually adapted copy of a
  reference, with or without a deliberately injected defect).
* **No safe execution of untrusted code.** `process` isolation is not a security boundary: native
  candidate code can read host files, the harness's environment or memory, open network connections and
  open its own database connection, whether or not the actor is given credentials. `strict`
  (container/VM) isolation is not implemented. See [SECURITY_MODEL.md](SECURITY_MODEL.md).
* **Not that the references are correct in general.** They passed the six scenarios over the
  seeds that were run. That is evidence about those schedules, not a proof.
* **Not complete coverage of interleavings.** Benchmark schedules are scripted or sampled with a
  seeded random policy at SQL-statement granularity. Bounded systematic exploration (unreleased in
  v0.3.0, included from v0.4.0) enumerates the schedules of **one** interleave directive with at most a
  given number of preemptions (default 1); crash points, other directives and deeper preemption are not
  explored, and there is no partial-order reduction or coverage guidance. An implementation can pass
  while still hiding a race no scenario or bound reaches.
* **Not liveness in general.** R4 checks a *bounded* property: under a scenario's stated
  assumptions (restart after a crash, drain v1, at most 3 retries per step), the migration reaches
  the contracted state. It says nothing about other assumptions.
* **Not root causes.** S1, S2, S4 and S6 diagnoses mean "consistent with". They are derived from
  relaxed models.
* **Not full determinism.** Logical reproducibility was measured and held for every re-run (see
  `RESULTS.md`). It assumes the same PostgreSQL version and configuration. The server's
  deadlock-victim selection and other timing-dependent behaviour are not controlled, and no current
  scenario deadlocks.

## Known technical weaknesses

1. **Statement granularity only.** Effects *inside* a statement, such as trigger bodies or
   multi-row UPDATE ordering, are not interleaved, and true parallel lock acquisition inside the
   server is not explored. Every scenario runs at READ COMMITTED.
2. **Permissive uncertain-operation windows.** An uncertain logical write may take effect in the
   union of its attempts' windows, which is more permissive than reality and could hide a
   violation.
3. **Strategy contracts are hand-encoded in the oracle.** S3 depends on a per-strategy agreement
   contract. A new strategy, for example from an external agent, must declare its contract, and
   whether it should be allowed to choose one is an open design question.
4. **The conventional suite is small and was written by the same author.** It is one plausible
   suite, not a standard.
5. **Performance.** A run takes about 4–10 s, dominated by Windows process start-up even with the
   warm pool. The full 180-run matrix plus re-runs and the conventional suite takes tens of
   minutes.
6. **Single worker.** The API executes runs one at a time. Parallel runs would need separate
   databases (already per-run) and attention to CPU contention effects on timing.
7. **Commands write to the results database they open.** Every command, including `report` and
   `serve`, adds missing tables to the database named by `GAUNTLET_RESULTS_DB`. Never point it at a
   file in `evidence/`; work on a copy (a used copy no longer matches its checksum).
8. **Experiment evidence checks are internal.** An experiment manifest is stored in the same database
   as the evidence it hashes, so `verify` detects accidental change, not deliberate tampering. Keep the
   export bundle hash externally for tamper evidence.
9. **POSIX limits unverified.** The POSIX `process`-mode path (`RLIMIT_AS`, `RLIMIT_CPU`, no
   process-count limit) runs in the Linux continuous-integration jobs, but the tests that check that the
   limits are enforced exist only for the Windows job object and are skipped on Linux.
10. **Docker path verified only in CI.** `docker-compose.yml` could not be exercised on the development
   machine; the Linux continuous-integration jobs run the test suite against it. All recorded results
   were produced against the portable local cluster (PostgreSQL 16.14), and the benchmark has not been
   re-executed on Linux.
11. **Observation privileges (A-02, addressed in v0.4.1).** External candidates'
   databases are observed by a per-run non-superuser role; the control connection and provisioning still use
   superuser credentials, a candidate can make its database unobservable (a candidate execution failure,
   never a pass), and the non-superuser-admin configuration is untested. See
   [SECURITY_MODEL.md](SECURITY_MODEL.md).
12. **Step-timeout attribution is a heuristic.** A candidate statement that exceeds the step timeout is
   classified `candidate_statement_timeout` only with supporting evidence, otherwise `infrastructure`
   (never a pass). A host that slows the server but not the harness can still make a CPU-bound statement
   look like the candidate's fault. See [SECURITY_MODEL.md](SECURITY_MODEL.md).

## Future work (ordered by expected value)

1. **Safe external-agent evaluation.** The candidate contract, snapshots, loader, task bundle and
   evaluation protocol exist (`docs/CANDIDATES.md`, `docs/AGENT_EVALUATION_PROTOCOL.md`). Missing: a
   verified isolation boundary (container or VM; requirements in `docs/SECURITY_MODEL.md`), then
   multiple runs per agent under equivalent conditions before any comparison is reported.
2. **Broader schedule exploration.** Extend the bounded search to several directives and crash
   points, add partial-order reduction or coverage-guided search over seeds, and report detection as a
   function of exploration budget.
3. **Broader task families.** Other partial-failure-sensitive tasks (outbox/queue consumers,
   idempotent payment APIs, sharded counters, cache invalidation), each with a model-based oracle.
4. Isolation-level variation (REPEATABLE READ, SERIALIZABLE), plus faults at the network layer
   (proxy-injected connection drops) as well as process kills.
5. Independent authoring of scenarios and mutants, to reduce authorship bias. Mutant-equivalence
   analysis.
6. Tighter oracle windows (per-attempt rather than union), plus an explicit witness linearization
   in the evidence for passing runs.
