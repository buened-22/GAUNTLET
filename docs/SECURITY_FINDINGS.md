# Audit findings index

Code comments, tests and documents cite audit findings by short identifiers (`A-01`, `FR-3`, `F-02`, …).
This page says which audit each identifier comes from, what it is, and where its current status is
recorded. It summarises existing records and does not change them.

**Who performed the audits.** The v0.1.0 audit is a self-audit by the author. The release-readiness audit
and the v0.4.1 security readiness audit were carried out by an AI assistant working for the author; each
report says so on its first lines. **None of these audits is an independent review**, and none has been
checked by a third party.

**Two different "F-02"s.** The identifier `F-02` is used by two unrelated audits. In code comments, tests,
the changelog and the release notes, `F-02` always means the **v0.4.1 security readiness audit** finding
about the Python audit-hook guard (below). Only the archived release-readiness audit, in finding FR-16,
uses "earlier findings F-02, F-03" for an older, unpublished series (see the last section).

## v0.4.1 security readiness audit (2026-11-13): `F-01`, `F-02`

An audit of the v0.4.1 release candidate before publication. Its report is not part of this repository;
both findings and their fixes are described in [../CHANGELOG.md](../CHANGELOG.md) (0.4.1, Security) and
[RELEASE_v0.4.1.md](RELEASE_v0.4.1.md#fixed-by-a-security-audit-before-this-release). Its other findings
are prototype limitations that the documentation already states.

| ID | Severity | Finding | Status |
|---|---|---|---|
| F-01 | High | With vite 5.4.11, any website open in the operator's browser could, while `npm run dev` was running, read local files and proxied API responses through the dashboard's development server | **Fixed in 0.4.1**: vite 6.4.6 and a hardened `frontend/vite.config.ts` ([SECURITY_MODEL.md](SECURITY_MODEL.md#the-api-and-dashboard)) |
| F-02 | Medium | The documentation said only native code could bypass the Python audit-hook guard; plain Python can switch most of it off | **Fixed in 0.4.1** (documentation): the guard is described as a tripwire for trusted code, not a control, and tests pin that behaviour (`backend/tests/test_candidate_sandbox.py`) |

## Audit hardening (2026-11): `A-01`, `A-02`

Findings addressed by the audit-hardening work released in v0.4.1 ([../CHANGELOG.md](../CHANGELOG.md),
0.4.1, Security). The report that first listed them is not published.

| ID | Finding | Status |
|---|---|---|
| A-01 | In v0.4.0, candidate code shared access to the actor's database connection | **Fixed in 0.4.1**: the harness owns every actor's database connection; the actor process has no connection, DSN or credentials. `process` mode is still not a security boundary ([SECURITY_MODEL.md](SECURITY_MODEL.md)) |
| A-02 | The harness observed a candidate's run database with a PostgreSQL superuser session | **Addressed in 0.4.1 for external candidates**: a per-run non-superuser observer role. The harness still holds superuser credentials, and built-in implementations are still observed as superuser ([LIMITATIONS.md](LIMITATIONS.md)) |

## Release-readiness audit (2026-11-05): `FR-1` to `FR-16`

The full report, with its own follow-up sections, is archived unchanged:
[archive/docs/FINAL_RELEASE_READINESS_AUDIT.md](archive/docs/FINAL_RELEASE_READINESS_AUDIT.md). Statuses below
are those recorded in that report's follow-ups or in later public documents; where none is recorded, the
table says so rather than guessing.

| ID | Subject (abridged from the report) | Status recorded |
|---|---|---|
| FR-1 | A-02: observation as superuser | Addressed for external candidates (report §13; A-02 above) |
| FR-2 | `process` mode is not a security boundary | Documented limitation ([../SECURITY.md](../SECURITY.md)) |
| FR-3 | Candidate-caused statement timeouts classified as infrastructure | Addressed (report §12; [../CHANGELOG.md](../CHANGELOG.md) 0.4.1); the evidence rule is a heuristic |
| FR-4 | A-02 disclosed in only one document | Documentation addressed (report §11) |
| FR-5 | Hardening uncommitted and unpublished | Released as v0.4.1 |
| FR-6 | Transient infrastructure failure in one full-suite run | No later status recorded |
| FR-7 | Stale wording in `docs/CANDIDATES.md` | Addressed (report §11) |
| FR-8 | Stale test counts | Addressed (report §11); current figures in [TESTING.md](TESTING.md) |
| FR-9 | Wall-clock test thresholds can flake | One test fixed ([../CHANGELOG.md](../CHANGELOG.md) 0.4.1, Fixed); other wall-clock bounds remain ([TESTING.md](TESTING.md)) |
| FR-10 | Weak spots in some tests | No later status recorded |
| FR-11 | Override of a private psycopg method | Fixed: public interfaces only ([../CHANGELOG.md](../CHANGELOG.md) 0.4.1, Changed; [THIRD_PARTY.md](THIRD_PARTY.md)) |
| FR-12 | Statement classification by first keyword | No later status recorded |
| FR-13 | 180-cell comparison script not in the repository; Linux paths unverified | Script added (`scripts/compare_benchmark.py`); Linux is exercised by continuous integration only |
| FR-14 | Performance overhead (informational) | No action needed (report) |
| FR-15 | Harness-owned methods patchable inside the candidate process (informational) | No later status recorded |
| FR-16 | Default superuser password and unauthenticated local API | Documented limitations ([../SECURITY.md](../SECURITY.md)) |

## v0.1.0 self-audit (2026-10-18): Findings 1 to 8

The author's audit of v0.1.0, kept unchanged at [GAUNTLET_VALIDATION_AUDIT.md](GAUNTLET_VALIDATION_AUDIT.md).
Findings 1 (oracle identity by idempotency key) and 2 (seed-driven schedule diversity) were fixed in 0.2.0.
The last recorded status of Findings 3 to 8 is "open" ([../PROJECT_SPEC.md](../PROJECT_SPEC.md), status as of
the v0.3.0 benchmark).

## Earlier unpublished findings cited as `F-02`, `F-03`

Finding FR-16 of the archived release-readiness audit says that the "default superuser password and an
unauthenticated local API (earlier findings F-02, F-03) remain". These two identifiers belong to an earlier
audit that has never been published, and they are **not** the v0.4.1 security readiness audit's `F-02`
above. FR-16 does not state which of the two is which. Both subjects are documented limitations
([../SECURITY.md](../SECURITY.md), [SECURITY_MODEL.md](SECURITY_MODEL.md)).

## Known dependency advisory (not yet fixed)

| Package | Advisory | Fixed in | Status |
|---|---|---|---|
| `opentelemetry-api` 1.45.1 (pinned in `backend/constraints.txt`) | CVE-2026-126360 / GHSA-mf5v-w3w3-f6cg: regular-expression denial of service in `TraceContextTextMapPropagator.extract()` when parsing a crafted `traceparent` value | 1.45.2 | Accepted for v0.4.1; the pin moves to 1.45.2 in the next engineering release |

GAUNTLET does not use OpenTelemetry itself. The affected function is reached through FastAPI's built-in
telemetry, which is active only when an OpenTelemetry provider is configured, and GAUNTLET configures none,
so `python -m gauntlet serve` in its default configuration does not use it. The API also listens on the
loopback interface only. Configuring OpenTelemetry tracing for the API would make the code path reachable.
