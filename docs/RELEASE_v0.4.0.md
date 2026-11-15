# Release v0.4.0: notes and publication checklist

**Status: released** as the annotated tag `v0.4.0` (commit `be9b742`) under the Apache License 2.0
(`LICENSE`). The verification results below are the release review of 2026-10-22 and describe that tagged
tree. Later, unreleased audit hardening is summarized in the README's release history.

## Release title

GAUNTLET v0.4.0: differential testing, bounded schedule exploration, failure minimization, experiment
reports and an external-candidate pipeline (research prototype)

## Summary

v0.4.0 adds analysis capabilities on top of the unchanged v0.3.0 benchmark. **No benchmark scenario,
oracle rule, reference implementation, mutant or recorded result changed.** The benchmark version stays
`0.3.0` (it is part of every schedule hash), and the frozen evidence for v0.1.0–v0.3.0 is byte-for-byte
unchanged. No AI coding agent has been evaluated, and no execution mode is a security boundary for
untrusted code.

## Added

* **Differential testing** (`gauntlet diff`, `/api/differential`). The same schedule runs against two or
  more implementations, with documented normalization (N1–N6) and four classifications: agreement,
  confirmed violation, unconfirmed disagreement, inconclusive.
* **Bounded schedule exploration** (`gauntlet explore`, `/api/explorations`). A preemption-bounded
  systematic search over one interleave directive. Its claims are exhaustive only within the stated
  bound. It ships with the fixture `F01_backfill_race_probe`.
* **Failure minimization** (`gauntlet minimize`, `/api/minimizations`). ddmin over schedule units with
  failure-signature matching, an independent replay, and an original-run integrity check.
* **Experiment reports** (`gauntlet experiment`, `/api/experiments`). Explicit metric definitions,
  traceable run ids, a hashed evidence manifest, `verify` and `export`.
* **External candidates** (`gauntlet candidate ...`, `/api/candidates`, `/api/evaluations`):
  * contract v1 and static validation;
  * content-addressed snapshots and a loader;
  * a `process` development sandbox:
    * Windows Job Object limits;
    * an allow-listed environment and per-actor workspaces;
    * an audit-hook guard;
    * a per-run restricted PostgreSQL role, with access to other runs' databases revoked.
  * `strict` mode always fails closed;
  * explicit evaluation statuses (completed, incomplete, interrupted, blocked, error) and CLI exit codes
    (0, 3, 4);
  * per-limit enforcement records;
  * provenance: a harness source fingerprint, PostgreSQL and package versions;
  * an agent task bundle and an evaluation protocol for a future study.
* **Watchdog and limits.** Run wall-clock deadlines, event limits, and timeouts recorded as such, never as
  passes.
* **Dashboard pages** for Differential, Exploration, Minimization, Experiments and Candidates.
* **Documentation:**
  * `docs/RESEARCH_METHODOLOGY.md`, `docs/SECURITY_MODEL.md`, `docs/REPRODUCIBILITY.md`, `docs/DEMO.md`,
    `docs/ANALYSES.md`, `docs/CANDIDATES.md`, `docs/AGENT_EVALUATION_PROTOCOL.md`;
  * `SECURITY.md`, `CONTRIBUTING.md`, `CITATION.cff`;
  * `research/`: technical report, results summary, demo script and captured transcript, open questions.
* `backend/constraints.txt`, which pins the exact verified dependency versions.

## Fixed during the release review

* **Minimization over-claimed 1-minimality.** If a single-unit removal from the final schedule ended in an
  infrastructure error, a timeout or a budget stop, it was cached as "rejected". The result could then
  claim "1-minimal with respect to unit deletion" even though that removal was never judged.
  * Now such removals void the claim, are listed in `one_minimal_unproven_units`, and are explained in
    `note`.
  * Regression tests: `test_unjudged_single_unit_removals_void_the_one_minimal_claim` (fails without the
    fix) and `test_judged_rejections_still_prove_one_minimality`.
  * Recorded minimizations made before this fix are not re-labelled. Any of them that reported 1-minimal
    while its final pass contained timed-out or errored removals over-states its minimality. The
    synthetic demonstration's minimization is unaffected: its final pass had no such removals, and it
    still reports 1-minimal after the fix.
* Earlier passes fixed:
  * isolation propagation;
  * the process-mode trust gate on every entry point;
  * cross-run PostgreSQL access;
  * evaluation completion status;
  * provenance: per-limit enforcement, the source fingerprint, line-ending-independent hashes,
    non-cached git state;
  * reports listing a manifest's claimed producer as an evaluated agent.

## Compatibility notes (no breaking changes to recorded data)

* The results database gains an `analyses` table, created automatically. Every command creates it in the
  database it opens, so never point `GAUNTLET_RESULTS_DB` at a file in `evidence/`.
* Run records gain a `decisions` field. Older records remain readable.
* `/api/runs` rejects external-candidate implementation ids (`ext:*`) with 422. Candidates run only through
  evaluations with an explicit isolation configuration.
* The package version in `backend/pyproject.toml` becomes `0.4.0`. The API's reported version remains the
  benchmark version, `0.3.0`. The frontend's `package.json` version (`0.1.0`) belongs to a private,
  never-published package and is not the release version; it was left unchanged to avoid touching the
  npm lockfile.

## Included files

This repository is new and starts with the v0.4.0 release commit; v0.1.0–v0.3.0 were developed in a separate
repository (see [HISTORY.md](HISTORY.md)). The release commit contains the complete v0.4.0 tree, which is the
v0.3.0 tree plus exactly these changes:
* every modified tracked file;
* every untracked, non-ignored file;
* the files created during the release review.

The authoritative list is produced by
`git status --short` and is staged path by path (never `git add -A`); see *Publication steps*.

## Excluded (git-ignored, must never be committed)

`.gauntlet/` (local PostgreSQL, results databases, candidate snapshots), `.venv/`, `frontend/node_modules/`,
`frontend/dist/`, `*.tsbuildinfo`, `__pycache__/`, `.pytest_cache/`, `*.egg-info/`, `.env*`.

## Proposed commit message

```text
release: GAUNTLET v0.4.0 (analyses, external-candidate pipeline, release documentation)

Adds differential testing, bounded schedule exploration, failure minimization,
experiment reports with hashed evidence manifests, and an external-candidate
pipeline with a process development sandbox (not a security boundary) and a
fail-closed strict mode. Fixes an over-claim of 1-minimality when single-unit
removals were never judged. Adds methodology, security model, reproducibility,
security policy, contribution guide, citation metadata and a research package.

No benchmark scenario, oracle rule, reference, mutant or recorded result changed;
BENCHMARK_VERSION stays 0.3.0 and evidence/ is byte-for-byte unchanged.
No AI coding agent has been evaluated.
```

Proposed annotated tag: `v0.4.0` with message `GAUNTLET v0.4.0`. The `v0.3.0` tag exists only in the development
repository and is not recreated here.

## Licence

**Decision: Apache License 2.0** (owner's choice). The standard, unmodified text is in `LICENSE`, and
`CITATION.cff` declares `license: Apache-2.0`. It applies to the whole repository, including `evidence/` and
the results records; no separate data licence was chosen. The options that were considered:

| Licence | Effect | Typical fit |
|---|---|---|
| Apache-2.0 | Permissive; explicit patent grant; requires attribution and notice of changes | Research software that companies may want to use or build on |
| MIT | Permissive and very short; no explicit patent grant | Maximum simplicity |
| BSD-3-Clause | Permissive; forbids using the author's name for endorsement | Similar to MIT |
| GPL-3.0 / AGPL-3.0 | Copyleft: derivatives must be released under the same licence (AGPL also covers network use) | Keeping derivatives open; may deter corporate use |
| No licence | All rights reserved | Showing work without permitting reuse |

A separate data licence (for example CC-BY-4.0 for `evidence/` and `docs/RESULTS*.md`) could still be
added later, but this release does not do so.

## Publication checklist

- [ ] Owner reviews this release diff and the README.
- [x] Owner chose a licence: Apache-2.0 (`LICENSE` added; `license: Apache-2.0` in `CITATION.cff`).
- [ ] Owner confirms the author name in `CITATION.cff` and the vulnerability-reporting route in `SECURITY.md`.
- [ ] Owner approves the version `0.4.0` and the commit message above.
- [ ] Commit (selected paths only), then the annotated tag `v0.4.0`.
- [ ] Create a remote (none exists) and push the branch and tags.
- [ ] Optional: an independent reproduction before announcing results.

## Publication steps (only after approval)

```bash
# stage reviewed paths explicitly (the review prepared this index; inspect it first)
git status --short
git diff --cached --stat
git commit -m "<message above>"
git tag -a v0.4.0 -m "GAUNTLET v0.4.0"
# create an empty repository on the hosting platform (private first is safer), then:
git remote add origin <url>
git push -u origin main
git push origin v0.4.0
```

**Before making anything public:**
* Everything in git history becomes public. This repository's history starts with the v0.4.0 commit, so the
  development repository's earlier commits are not published; the three evidence databases (about 80 MB)
  are. Making a repository private again, or deleting a file later, does not remove copies that
  others have already cloned, forked, cached or indexed.
* The release review's scan found no secrets or personal paths in the tracked or proposed files, including
  the evidence databases. That scan was local and pattern-based, not comprehensive: no dedicated secret
  scanner was available.
* The commit author name and e-mail address in git history are published with the repository.
* Consider publishing privately first, reviewing the rendered repository, and then changing visibility.

## Verification results (release review, 2026-10-22)

Environment: Windows 11, Python 3.14.0, Node 24.11.0, PostgreSQL 16.14 (local portable cluster). All runs by
the author; not independently reproduced.

| Check | Command | Result |
|---|---|---|
| Backend suite, working tree | `cd backend && python -m pytest -q` | **409 passed, 0 failed** (67 need PostgreSQL), 1 Starlette deprecation warning, exit 0 |
| Minimization tests incl. the new regressions | `python -m pytest -q tests/test_minimize.py` | 16 passed |
| Frontend, working tree | `npm run typecheck`, `npm run build` | exit 0, exit 0 |
| Clean copy (in the development repository): `git archive v0.3.0` + the proposed changes, fresh venv | `pip install -c backend/constraints.txt -e "backend[dev]"` | exit 0; all 31 installed versions identical to `constraints.txt`; package version 0.4.0 |
| Backend suite, clean copy | `python -m pytest -q` | **409 passed, 0 failed**, exit 0 |
| Frontend, clean copy | `npm ci && npm run typecheck && npm run build` | exit 0 |
| README quick start, clean copy | `gauntlet diff --impls ref_a_trigger,m1_stale_backfill --scenario S02_backfill_vs_legacy_writes --seed 1` | `CONFIRMED_VIOLATION`, exit 0 |
| Synthetic demo (DEMO.md steps 4–8), scratch database | see `research/demo-transcript.md` | all steps as documented: confirmed violation / agreement; exploration 18 schedules, 13 pass / 5 fail, 1 class, replay reproduced; minimization 104 → 19, 1-minimal; replay hashes identical; evaluation blocked without trust (exit 3) and with `strict` (exit 3); trusted synthetic evaluation completed; `verify` 5/5 unchanged; blocked summary 409; direct candidate run 422 |
| Benchmark reconciliation | recomputed from the raw rows of `evidence/results-bench-eed0c47427.sqlite3`, opened read-only | 180 distinct cells; 121 pass / 59 fail; 108 invariant FAIL entries; 0 inconclusive, 0 infrastructure errors; 60 re-run rows, 60/60 identical; 8/8 mutants killed by their intended invariant; references 36/36 |
| Report reproducibility | regenerate an experiment report from its stored configuration and evidence | markdown SHA-256 identical to the recorded hash |
| Frozen evidence | `(cd evidence && sha256sum -c *.sha256)` | 4 OK, before and after the review; all protected paths byte-identical (manifest compared) |
| Secret / personal-data scan | local pattern scan of all tracked and proposed files (including the SQLite evidence, as bytes) | no keys, tokens, private keys, personal names, home paths or e-mail addresses; connection strings are placeholders, the documented local dev default, or fake test credentials. **Not comprehensive**: no dedicated scanner was available |

The clean copy was assembled before later documentation and licensing edits to `README.md`,
`CONTRIBUTING.md`, `CITATION.cff`, this file, and the new `LICENSE`. No code, test, dependency or build
configuration file changed after it was assembled.

Not performed:
* Linux or macOS runs.
* Docker Compose.
* A fresh `gauntlet pg install` / `pg init` in the clean copy (it used the existing local cluster).
* A dependency vulnerability audit (`pip-audit` would send the dependency list to an external service;
  not authorized).
* A comprehensive secret scan.
* Independent reproduction.
* Any real-agent evaluation (deliberately: no isolation boundary).
