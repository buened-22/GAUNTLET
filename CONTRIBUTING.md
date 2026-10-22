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

## Development setup

```bash
python -m venv .venv && source .venv/bin/activate        # Windows (Git Bash): source .venv/Scripts/activate
pip install -c backend/constraints.txt -e "backend[dev]"
python -m gauntlet pg install && python -m gauntlet pg init && python -m gauntlet pg start
cd backend && python -m pytest -q                        # unit + PostgreSQL integration tests
cd ../frontend && npm ci && npm run typecheck && npm run build
```

## Pull requests

* Keep changes small and focused; describe what changed, why, and how you verified it.
* Include tests for behaviour changes; a test for a fixed defect should fail without the fix.
* Run the full backend suite and the frontend typecheck and build before submitting.
* Do not commit `.gauntlet/`, results databases, virtual environments, `node_modules/` or build output.

## Licence

GAUNTLET is licensed under the [Apache License 2.0](LICENSE). Unless you state otherwise, any contribution
you intentionally submit is licensed under the same terms (section 5 of the licence).
