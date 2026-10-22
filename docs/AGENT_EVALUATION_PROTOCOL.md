# Protocol: evaluating externally generated migration implementations

Status: **protocol defined; no agent has been evaluated.** Nothing in this repository is agent output.
This document fixes the design before any data exists, so that later deviations can be recorded as
deviations (GAUNTLET experiments keep versioned plans).

## 1. Task given to the agent

The agent receives exactly the files in [`agent_task/`](agent_task/) (`TASK.md`) and the public contract
module sources `backend/gauntlet/impls/base.py`, `backend/gauntlet/impls/legacy.py` (the legacy app it
must coexist with) and `backend/gauntlet/money.py`. It must produce one candidate directory satisfying
[CANDIDATES.md](CANDIDATES.md) §1.

**Withheld** (to avoid leaking evaluation cases): the oracle (`gauntlet/oracle/`), invariant definitions
beyond the plain-language requirements in TASK.md, the scenario catalog and fixtures, schedules and seeds,
the reference implementations and mutants, the conventional suite, all results and evidence, and this
repository's tests. If a study deliberately reveals any of these, the plan must say so and the claims
must be narrowed accordingly.

## 2. Conditions to record per study

* Agent / model identifier and version (or "unknown"), provider, date; temperature and other sampling
  settings where available.
* SHA-256 of the exact prompt text, of any system prompt, and of the provided context bundle.
* Tools available to the agent (editor, shell, interpreter, PostgreSQL access or not) and network access
  (default: none). Allowed dependencies: none beyond the standard library and the contract modules.
* Wall-clock and token budgets per attempt.

## 3. Attempts, retries and selection

* Run **N independent attempts** (fresh session, same prompt, no shared state), N fixed in the plan
  (at least 5 for any statement about one configuration; more for comparisons).
* No cherry-picking: every attempt's output is registered as-is, in attempt order, including outputs that
  fail validation (recorded as *invalid*, counted in the denominator of "produced a valid candidate").
* A generation that times out or produces nothing is a *failed generation*, counted, not retried, unless
  the plan defines a retry rule (e.g. one retry after an infrastructure failure of the agent service),
  applied identically to all attempts and recorded.
* If the protocol allows the agent to see its own validation errors and resubmit, each resubmission is a
  separate, numbered candidate linked to its attempt.

## 4. Binding outputs to attempts

* The operator copies each attempt's directory, registers it (`gauntlet candidate register`) and records
  `(attempt number, candidate_id, source_hash)` in the experiment plan before evaluating anything.
* The source hash is the identity. Any later edit produces a different candidate id; only the registered
  snapshot is evaluated. Post-hoc edits (for example to make a candidate load) are new candidates and are
  reported separately, never substituted.
* The manifest's `producer` field is *claimed* metadata; the plan and the operator's log are the record.

## 5. Evaluation

Each registered candidate: `gauntlet candidate evaluate` with the scenarios, seeds, references, limits,
exploration bounds and minimization budget fixed in the plan, isolation `process` (or `strict` once
available), on one machine and one PostgreSQL version. Infrastructure failures are re-run once under the
plan's rule and reported; candidate execution failures and timeouts are outcomes, not re-run.

## 6. Metrics

Per attempt: valid candidate (yes/no); per scenario × seed: pass / fail (which invariants) / candidate
execution failure / timeout / infrastructure; differential classification against each reference;
exploration failure classes within bounds; minimized failure sizes. Across attempts: proportions with
explicit denominators (all attempts; valid candidates), with uncertainty only as allowed by the sampling
design (independent attempts of one configuration are a sample of that configuration's output
distribution under these conditions, not of "the model" in general).

## 7. Runbook for the first genuine run (not yet executed)

Readiness as of the latest audit: **blocked** on two prerequisites, neither available on the development
machine (Windows 11 Home, no administrator rights, no Docker/Podman, no WSL, no Windows Sandbox):

1. **A verified isolation boundary.** `process` mode is not one, and `strict` is not implemented. Smallest
   safe option: run GAUNTLET (repository, PostgreSQL, evaluation) entirely inside a disposable VM or cloud
   instance that holds no credentials or other data, with outbound network disabled after setup, and
   destroy it afterwards. Inside that VM, `--trusted-dev-env` is then a true statement. Implementing a
   container backend for `strict` is the alternative; it must be verified (no host mounts except the
   snapshot read-only, no network, resource quotas) before it is called a boundary.
2. **Authorized access to an agent**, with its identifier and version known, and permission to send it the
   task bundle (TASK.md and the three contract modules only; never the repository).

Then, in order:

1. Write the study plan and register it before generating anything:
   `python -m gauntlet experiment create plan.json` with `"experiment_type": "confirmatory"`,
   `"preregistered": true`, the hypotheses, scenarios, seeds and the attempt count N.
2. For attempt k = 1..N: start a fresh agent session; give it exactly the task bundle and the prompt
   (record SHA-256 of both); save its output directory untouched as `attempt-k/`; record agent id,
   version, settings, start/end time and any failure in the study log.
3. Inside the VM: `python -m gauntlet candidate validate attempt-k` (record the result; an invalid output is
   a counted outcome, not repaired), then `candidate register attempt-k` and record
   `(k, candidate_id, source_hash)` in the study log before evaluating anything.
4. `python -m gauntlet candidate evaluate --id <candidate_id> --isolation process --trusted-dev-env ...` with
   the plan's scenarios, seeds, references and budgets, `--out attempt-k-summary.md`.
5. Report every attempt (valid or not) with the evaluation and experiment ids and the manifest checks.

A single attempt is a demonstration of the pipeline on agent output, not evidence about the agent.

## 8. What a study can and cannot support

Can support: "under this prompt, context, tool set and budget, k of N attempts produced candidates that
violated invariant X under GAUNTLET scenario Y", with the evidence manifest.

Cannot support: claims about agents or models in general, about other tasks or databases, about real
production safety, or about root causes of failures; claims of correctness from passing runs; and any
statistical claim from a single candidate.
