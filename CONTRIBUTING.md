# Contributing

Thank you for your interest. GAUNTLET is a research prototype maintained by one author; contributions
that improve its validity, reproducibility or safety are the most valuable.

## Most useful contributions

1. **Independent reproduction.** Follow [docs/REPRODUCIBILITY.md](docs/REPRODUCIBILITY.md) on your own
   machine and report the results (matching or not) in an issue, with your platform, Python, PostgreSQL
   versions and the verification output.
2. **Oracle and scenario review.** Look for cases where the oracle could accept an incorrect history or
   reject a correct one (see [docs/INVARIANTS.md](docs/INVARIANTS.md)), or where scenarios are shaped
   around the mutants.
3. **Isolation.** A tested container or VM boundary for `strict` mode (see the requirements at the end of
   [docs/SECURITY_MODEL.md](docs/SECURITY_MODEL.md)).
4. **Independently authored mutants or scenarios**, to reduce authorship bias.

## Rules that protect the research record

* **Never modify `evidence/`**, the historical `docs/RESULTS*.md` files, `docs/GAUNTLET_VALIDATION_AUDIT.md`
  or `audit/`. They are frozen records; their checksums are verified with
  `(cd evidence && sha256sum -c *.sha256)`.
* Changes to the oracle (`backend/gauntlet/oracle/`), scenarios (`scenarios/catalog.py`), references or
  mutants change what the benchmark measures. They require a new `BENCHMARK_VERSION`, a new benchmark run
  written to a new results file, and a written rationale. Never edit them to make a result look better.
* Never weaken an assertion, delete a failing test or loosen a security check to get a green suite. If a
  test is wrong, explain why in the change.
* Do not add claims about AI agents, security or generality that the recorded evidence does not support.

## Security expectations

* GAUNTLET does not sandbox untrusted code ([SECURITY.md](SECURITY.md)). Do not add wording, defaults or
  examples that suggest otherwise.
* Never make an isolation mode fall back to a weaker one. A mode that cannot provide what it promises must
  refuse to run.
* Test fixtures under `backend/tests/candidates/` are hand-written and labelled synthetic. Do not add
  AI-generated or otherwise untrusted code to the repository as a fixture.
* A security control is documented as enforced only if a test exercises it on the platform named.
* Do not commit credentials, tokens, `.env` files, personal data or machine-specific paths. The local
  PostgreSQL password `gauntlet` is a published development default, not a secret.
* Report vulnerabilities privately, as described in [SECURITY.md](SECURITY.md).

## Development setup

Requirements: Python 3.11 or later (developed and verified on 3.14), a PostgreSQL 16 server, and Node 18 or
later for the dashboard and for the portable PostgreSQL download. Run the commands from the repository
root.

```bash
python -m venv .venv && source .venv/bin/activate        # Windows (Git Bash): source .venv/Scripts/activate
pip install -c backend/constraints.txt -e "backend[dev]"
python -m gauntlet pg install && python -m gauntlet pg init && python -m gauntlet pg start
python -m gauntlet pg check                              # must print "is_pg16": true
(cd backend && python -m pytest -q)                      # unit + PostgreSQL integration tests
(cd frontend && npm ci && npm run typecheck && npm run build)
bash scripts/run_demo.sh                                 # end-to-end check of the engine and the safety gates
```

With Docker, `docker compose up -d --wait` replaces the three `gauntlet pg` commands.

## Running tests

```bash
cd backend
python -m pytest -q                 # everything
python -m pytest -q -m "not pg"     # tests that need no database
python -m pytest -q -m pg           # PostgreSQL integration tests only
```

Tests marked `pg` are skipped, with the reason printed, when no PostgreSQL 16 server is reachable at
`GAUNTLET_PG_DSN`. A run in which they were skipped is not a full run: check the summary line for skips
before reporting a result. The full suite takes roughly 10 to 30 minutes, because every database test
starts real processes against a real server.

No formatter or linter is configured for the Python code. Follow the style of the file you are editing. The dashboard is checked with `npm run typecheck`.

Continuous integration (`.github/workflows/ci.yml`) verifies the evidence checksums, runs the full backend
suite on Linux and Windows against PostgreSQL 16, runs the demonstration script, and typechecks and builds
the dashboard.

## Pull requests

* Keep changes small and focused; describe what changed, why, and how you verified it.
* Include tests for behaviour changes; a test for a fixed defect should fail without the fix.
* Run the full backend suite and the frontend typecheck and build before submitting, and state the
  platform, Python and PostgreSQL versions you ran them on.
* Add an entry to [CHANGELOG.md](CHANGELOG.md) under *Unreleased*.
* Do not commit `.gauntlet/`, results databases, virtual environments, `node_modules/` or build output.

Everyone taking part is expected to follow the [code of conduct](CODE_OF_CONDUCT.md).

## Licence

GAUNTLET is licensed under the [Apache License 2.0](LICENSE). Unless you state otherwise, any contribution
you intentionally submit is licensed under the same terms (section 5 of the licence).

Contribute only code you wrote or have the right to submit under Apache-2.0. Do not copy code from
dependencies or other projects into the repository, including small adapted fragments: several dependencies
use other licences (psycopg is LGPL-3.0, see [docs/THIRD_PARTY.md](docs/THIRD_PARTY.md)). Use a
library's documented public interface instead of overriding its private methods. If third-party code must be
included, say so in the pull request, keep its licence and notices, and record it in `NOTICE` and
`docs/THIRD_PARTY.md`.
