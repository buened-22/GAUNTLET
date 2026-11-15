# Third-party software

This page records what GAUNTLET depends on and under which licences, as checked on 2026-11-12. It is a
description for readers, not legal advice.

## What is in this repository

Everything in this repository is original to the project and is licensed under the
[Apache License 2.0](../LICENSE); the copyright notice is in [NOTICE](../NOTICE). That includes the source
code, the documentation, the test fixtures under `backend/tests/candidates/` (hand-written, labelled
synthetic) and the frozen benchmark evidence in `evidence/`, which is data produced by running GAUNTLET.

The repository contains **no vendored or copied third-party source code**, no bundled binaries, fonts or
images, and nothing loaded from a content-delivery network. `frontend/package-lock.json` is a generated list
of package names, versions and checksums. [CODE_OF_CONDUCT.md](../CODE_OF_CONDUCT.md) links to the
Contributor Covenant instead of reproducing it.

The code that sends statements to PostgreSQL ([`backend/gauntlet/actor/connection.py`](../backend/gauntlet/actor/connection.py))
uses only psycopg's documented public interfaces (`psycopg.pq` and `psycopg.adapt`). During development
between v0.4.0 and v0.4.1, an unreleased version of that module overrode one private method of psycopg's
cursor with an adapted copy of about a dozen lines of psycopg's own code. It was never part of a tagged
release. Before v0.4.1 it was removed and replaced by an implementation written for this project against
the public interfaces; a test (`test_the_statement_path_uses_only_public_psycopg_interfaces`) keeps
psycopg's private names out of that module.

Neither the candidate evaluated in [../research/gemini-attempt2-results.md](../research/gemini-attempt2-results.md)
nor any other AI-generated candidate is included in the repository.

## What you install separately

None of the following is distributed with GAUNTLET. `pip`, `npm` or Docker download it onto your machine,
and each item stays under its own licence.

### Python packages (direct dependencies)

| Package | Used for | Licence |
|---|---|---|
| psycopg, psycopg-binary | PostgreSQL driver (the binary package includes libpq) | LGPL-3.0-only |
| FastAPI | HTTP API | MIT |
| Uvicorn | HTTP server | BSD-3-Clause |
| Pydantic | request validation | MIT |
| pytest (development) | tests | MIT |
| HTTPX (development) | API tests | BSD-3-Clause |

Their own dependencies, as pinned in `backend/constraints.txt`, are under MIT, BSD, Apache-2.0, PSF-2.0 or
MPL-2.0 (certifi) licences.

**About psycopg.** psycopg is licensed under the GNU Lesser General Public License, version 3. GAUNTLET
imports it as an unmodified library that you install yourself, which the LGPL permits for software under
any licence, and GAUNTLET's own code stays under Apache-2.0. If you redistribute GAUNTLET together with
psycopg, for example in a container image or an installer, the LGPL's conditions apply to the psycopg part
of what you distribute.

### Dashboard (npm)

React, React DOM, Vite (6.4.6) and `@vitejs/plugin-react` are MIT-licensed; TypeScript is Apache-2.0. Of the
120 packages in `frontend/package-lock.json`, 111 are MIT, 5 ISC, 2 Apache-2.0, 1 BSD-3-Clause and
1 CC-BY-4.0 (`caniuse-lite`, a data package used at build time). Five of them end up in the built dashboard:
React, React DOM and their dependencies `scheduler`, `loose-envify` and `js-tokens`, all MIT. The rest are
development and build tools.

### PostgreSQL

* `python -m gauntlet pg install` downloads PostgreSQL 16 binaries through the npm package
  `@embedded-postgres/<platform>` (MIT), which repackages PostgreSQL (PostgreSQL License).
* `docker-compose.yml` uses the `postgres:16` image from Docker Hub.

### Continuous integration

`.github/workflows/ci.yml` uses the GitHub-maintained actions `actions/checkout`, `actions/setup-python` and
`actions/setup-node` (MIT).
