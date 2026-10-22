# External candidates: contract, loader, isolation, evaluation

This is the operator guide for evaluating a migration implementation produced outside GAUNTLET (for
example by an AI coding agent). **No external AI coding agent has been evaluated yet.** The workflow has
been exercised only with clearly labelled synthetic fixtures (`backend/tests/candidates/`), which are
hand-adapted copies of reference A, not agent output. The controlled protocol for a real agent study is
in [AGENT_EVALUATION_PROTOCOL.md](AGENT_EVALUATION_PROTOCOL.md).

## 1. Candidate contract (version `1`)

| Item | Rule | Stability |
|---|---|---|
| Layout | A directory with `gauntlet_candidate.json` plus `.py` (and optional `.json`, `.md`, `.txt`) files; at most 64 files, 256 KiB per file, 1 MiB total, depth 4 | stable |
| Manifest | `contract_version` (`"1"`), `name` (`^[a-z0-9][a-z0-9._-]{0,63}$`), `entry` `{"module", "class"}`, `strategy` (`"A"` one column authoritative and kept in sync on every write, `"B"` both columns written by V2 and reconciled), optional `description`, optional `producer` (recorded as *claimed*, never verified). Unknown keys are rejected | stable |
| Entry class | Implements `v2_create_order`, `v2_set_total`, `v2_add_charge`, `v2_get_order`, `migrator_expand`, `migrator_backfill_batch`, `migrator_finalize`, `migrator_contract` with the signatures of `gauntlet/impls/base.py`: `(self, db: Db, args: dict) -> dict`, raising `AppError(code)` for client-visible failures | stable |
| Harness-owned | `v1_*` (the legacy application) and `app_*` (instance registration) are supplied by the harness and identical for every implementation; a candidate that defines them is rejected | stable |
| Database access | Only through the `db` handle (`query`, `one`, `execute`, `transaction`), which gates every statement | stable |
| Imports | Standard library except process, network, FFI, threading and file-tree modules; `gauntlet.impls.base`; `gauntlet.money`; the candidate's own modules. References, mutants, oracle, scenarios and database drivers are rejected. `eval`, `exec`, `compile` and `__import__` calls are rejected | experimental (the allow-list may change) |
| Schema | Harness-owned P0 schema: `orders(id, total NUMERIC(12,2))`, `processed_requests(key, response)`, `app_instances(instance_id, version)`, `migration_state(id, stage, backfill_cursor)`; the target is `orders.total_cents BIGINT` with `total` dropped after contract | stable |

Static validation (`gauntlet candidate validate DIR`) runs no candidate code. It rejects missing files
or entry points, malformed or unknown manifest keys, unsupported contract versions, symbolic links,
junctions and other reparse points, paths resolving outside the directory, case collisions, non-UTF-8
files, disallowed file types, oversized packages, syntax errors, forbidden imports, missing required
methods, harness-owned methods, and module names that would shadow the standard library or harness
modules. Passing validation does not make a candidate trustworthy.

Errors are distinguished as:

* **semantic failure**: the run completed and the independent oracle FAILs an invariant (verdict `fail`);
* **client-visible error**: a candidate method raised (`AppError`, a database error, or any exception);
  this is ordinary behaviour judged by the oracle, not a crash;
* **candidate execution failure** (`candidate_execution_failure`): the candidate's actor process died,
  broke the protocol, stopped responding after loading, or the candidate failed to load;
* **timeout** / **limit_exceeded**: a configured limit was reached;
* **infrastructure** / **isolation_unavailable** / **candidate_rejected**: the harness, the requested
  isolation or the snapshot check failed; the candidate was not (fully) executed.

None of the non-semantic outcomes is ever a pass.

## 2. Snapshots and provenance

`gauntlet candidate register DIR` validates the directory and copies the normalized files into
`.gauntlet/candidates/<candidate_id>/src` (git-ignored; override with `GAUNTLET_CANDIDATES_DIR`).

* **Canonical hash**: SHA-256 over `{"contract_version", "files": [[path, sha256, length], ...]}` with
  files sorted by relative POSIX path; text is UTF-8 with a leading BOM removed and line endings
  normalized to LF; `__pycache__/`, `.git/`, `*.pyc`, `*.pyo` and `.DS_Store` are excluded (and listed);
  timestamps, permissions and the directory name are ignored. Any content change changes the hash.
* **Candidate id**: `cand-` + the first 16 hex digits of the hash. Identical source gives the same id;
  changed source a new id. Snapshots are written once (temporary directory, then rename), made read-only
  and never overwritten. Only the source directory's base name is recorded, never a host path.
* The snapshot is re-hashed before every run, on the host and again inside the actor process; a
  modified snapshot is refused (`candidate_rejected`).
* Every run records the candidate id, source hash, contract and loader versions, the isolation record
  (mode, configuration, the operator's trusted-environment declaration, how each limit is enforced on this
  platform, applied controls, what is not enforced), the per-run database role, the oracle, engine and
  reference fingerprints, the harness source fingerprint (all `gauntlet/**/*.py`, so uncommitted changes
  are identified) and whether it changed since the executing process started, PostgreSQL version, seeds,
  schedule hash and the actors' captured output size. Source fingerprints are computed with line endings
  normalized to LF, so the same commit fingerprints identically on CRLF and LF checkouts. Evaluations
  additionally record start and end times, the harness provenance (git revision and whether the backend
  differs from it, harness source fingerprint, benchmark version, oracle and engine hashes, PostgreSQL,
  Python, platform and package versions), the SHA-256 of the agent task bundle (`docs/agent_task/TASK.md`
  and the three contract modules), every run, comparison, exploration, minimization and experiment id,
  steps that did not finish, and the completion status.
* Reports never list a manifest's claimed producer as an evaluated agent: they state that external
  candidates were evaluated from snapshots and that their producer is not verified.
* **Claimed, not verified:** the manifest's `producer` (agent, model, attempt). GAUNTLET cannot verify who or
  what produced a candidate.
* **Not recorded by GAUNTLET:** the prompt actually given to an agent, the agent's configuration, tools and
  network settings, and the attempt number. The operator records these in the study log
  (see [AGENT_EVALUATION_PROTOCOL.md](AGENT_EVALUATION_PROTOCOL.md)).
* Dependencies are declared with lower bounds only (`backend/pyproject.toml`), which admit materially newer
  versions (a clean install without constraints resolved FastAPI 0.158.0 instead of the verified 0.143.3).
  `backend/constraints.txt` pins the exact verified environment: install with
  `pip install -c backend/constraints.txt -e "backend[dev]"` for reproducible runs. The versions actually
  used are also in each evaluation's harness provenance.

## 3. Isolation modes and their real boundaries

| Mode | What it is | Status here |
|---|---|---|
| `strict` | Container isolation (separate file system, no network, quotas) | **Not implemented.** Always fails closed (`isolation_unavailable`); no container runtime is installed on the development machine |
| `process` | Development sandbox. **Not a security boundary** | Implemented and tested on Windows. Refuses to run (fails closed) unless the operator declares the host a trusted, disposable development environment: CLI `--trusted-dev-env`, API / config `isolation.trusted_environment = true`; the declaration is recorded in every run |

`process` applies, per actor process: an allow-listed environment (no API keys, tokens, database
credentials or user variables); its own temporary working directory (also `TEMP`/`TMP`/`HOME`); the base
interpreter with `-s`; on Windows a Job Object with kill-on-close, an active-process limit of 1 (no child
processes), a memory limit (default 512 MB) and a CPU-time limit (default 120 s); on POSIX a process group
with `RLIMIT_AS`/`RLIMIT_CPU` (not exercised here) and **no** process-count limit. Each run's isolation
record states, per limit, whether and how it was enforced on that platform. Inside the actor, before candidate code is imported,
an audit-hook guard denies writes outside the workspace, process creation, socket operations and ctypes.
The actor connects to PostgreSQL as a per-run role that owns only its run database (not a superuser; no
CREATEDB, CREATEROLE, REPLICATION or BYPASSRLS). CONNECT and TEMPORARY are revoked from PUBLIC on every run
database (candidate and reference runs alike), so a candidate's role cannot connect to another run's
database; it can still connect to the cluster's `postgres` maintenance database, where it has no
privileges beyond PUBLIC defaults. Captured stderr is bounded
(default 1 MB, then counted only); protocol lines longer than 4 MiB are a protocol violation. At most two
candidate runs execute concurrently (`GAUNTLET_MAX_CANDIDATE_RUNS`). The run's wall-clock limit is
enforced by the watchdog; the run database, the role, the workspace and the job are removed on success,
failure, timeout and cancellation.

**Not guaranteed by `process` mode:** OS-level read isolation (a candidate can read anything the host user
can read, including this repository, the oracle source and the results databases); OS-level write
isolation (the audit-hook guard denies Python-level writes outside the workspace, but native code running
as the host user can write wherever that user can, including evidence files and other candidates'
snapshots, although snapshot and evidence hashes would then fail verification); OS-level network
isolation (native code can open sockets); protection against native code that defeats the audit hook.
**Untrusted code, including any real AI-generated candidate, must not be run in `process` mode on a
machine you care about.** Run it only inside a disposable VM until a verified `strict` backend exists.

Every candidate run executes with the isolation configuration of the request that started it: an
evaluation passes it to all of its benchmark, exploration and minimization runs, and a replay or a
minimization of an existing candidate run re-uses the configuration recorded on that run.

Launch safety: processes are started with argument arrays (no shell); candidate-supplied strings are
never used as paths or arguments on the host; the API accepts candidate ids (validated format) only,
never file-system paths (registration is CLI-only).

## 4. Usage

```bash
cd backend
python -m gauntlet candidate isolation                      # what this host can provide
python -m gauntlet candidate validate /path/to/candidate    # static checks, no execution
python -m gauntlet candidate register /path/to/candidate    # immutable snapshot, prints the candidate id
python -m gauntlet candidate verify cand-0123456789abcdef   # re-hash the snapshot
# process isolation runs only with the explicit trusted-environment declaration (trusted code only):
python -m gauntlet candidate evaluate --id cand-0123456789abcdef --isolation process --trusted-dev-env \
    --scenarios S01_baseline_migration,S02_backfill_vs_legacy_writes --seeds 1,2,3 \
    --refs ref_a_trigger,ref_b_dualwrite --time-limit 180 \
    --explore-max-schedules 40 --explore-max-preemptions 1 --minimize-budget 40 --out summary.md
```

API: `GET /api/isolation`, `GET /api/candidates`, `GET /api/candidates/{id}`, `POST /api/evaluations`,
`GET /api/evaluations/{id}`, `GET /api/evaluations/{id}/summary.md`, cancellation through
`POST /api/analyses/{id}/cancel`. The dashboard's *Candidates* page lists snapshots and evaluations.

`evaluate` exit codes: 0 completed, 3 blocked (isolation unavailable or not declared, snapshot invalid),
4 incomplete or interrupted. An evaluation is `completed` only if every comparison ran, no requested
exploration or minimization ended in `error` or `interrupted`, the report is complete and its evidence
manifest verifies.

Ad-hoc differential, exploration and minimization requests through the API do not accept an isolation
configuration; for an external candidate they therefore fail closed (`isolation_unavailable`). Use the
evaluation workflow (CLI or `POST /api/evaluations`), or minimize / replay an existing candidate run, which
re-uses that run's recorded configuration.

## 5. What an evaluation does and how to read it

For every selected (scenario, seed) the references and the candidate run the same schedule document,
each on its own database (`differential.run_differential`): these are the candidate's benchmark runs and
their matched controls. Then (optional) bounded exploration on `F01_backfill_race_probe`, minimization of
up to `max_failures` failing scenarios, and an experiment report with the stopping rule (references +
candidate × scenarios × seeds) and a manifest check. The evaluation is `completed` only if every
comparison ran, the report is complete and the manifest verifies; otherwise `incomplete`,
`interrupted` or `blocked`.

The summary separates **observations** (outcomes per scenario, classifications, exploration coverage
within its stated bounds, minimization results, manifest status) from **interpretation**. A FAIL is a
confirmed invariant violation under these schedules; the oracle's diagnosis is what the evidence is
consistent with, not a proven cause. Passing is evidence about these schedules only. One candidate is
one sample.

Intentional difference in execution conditions: the candidate runs with a restricted database role and
sandboxed actors, the references with the harness role. With the synthetic fixtures this changes no
logical result (the adapted copy of reference A produces trace and outcome hashes identical to
reference A; the stale-backfill copy identical to mutant m1).

## 6. Reproducing

1. `python -m gauntlet candidate verify <id>` must report the recorded source hash.
2. Re-run `candidate evaluate` with the configuration in the evaluation record, or replay any run with
   `python -m gauntlet replay <run_id>`; logical hashes are compared, wall-clock timing is not.
3. `GET /api/experiments/{id}/verify` re-hashes the report's evidence.

## 7. Adding an external agent integration later

Keep the agent outside GAUNTLET: the agent receives `docs/agent_task/TASK.md` (and nothing from
`gauntlet/oracle`, `gauntlet/scenarios`, `gauntlet/impls/reference_*`, `mutants.py` or the evidence),
writes a candidate directory, and the operator registers each attempt's output unchanged. Record the
agent identity, version, prompt hash and attempt number in the manifest's `producer` (claimed metadata)
and in the experiment plan; the snapshot hash is the binding identity. Do not create accounts, incur
costs or send repository contents to an external service without authorization.
