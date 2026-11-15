# Security policy

GAUNTLET is a research prototype. It is not intended for production use.

> **Warning: GAUNTLET does not sandbox untrusted code.** No execution mode in this version is a security
> boundary. Do not run code from an AI model, or from any other source you do not trust, on a machine you
> care about. If you need to evaluate such code, run GAUNTLET itself inside a disposable virtual machine
> that has no network and no shared folders, and treat everything that comes out of that machine as
> untrusted data.

This page is the short version. The full analysis, with the status of every control, is
[docs/SECURITY_MODEL.md](docs/SECURITY_MODEL.md).

## Threat model in brief

GAUNTLET runs migration implementations against PostgreSQL. Built-in implementations (the references and
the faulty variants) are hand-written and trusted. *External candidates* are implementations produced
outside the project, possibly by an AI coding system, and must be assumed malicious or defective: arbitrary
Python, and through Python native code, that may read or write host files, read the oracle, open network
connections, start processes, exhaust resources, escalate database privileges or tamper with results.

Out of scope: an attacker who already has a shell on the host, a malicious operator, a compromised Python
or PostgreSQL build, and supply-chain attacks on GAUNTLET's dependencies.

## Execution modes

| Mode | Status | Use it for |
|---|---|---|
| Built-in implementations | Supported. No isolation applied or claimed | The references and faulty variants in this repository |
| `process` | Supported **for trusted code only**. A development sandbox, **not a security boundary** | Your own code and the labelled synthetic fixtures in `backend/tests/candidates/` |
| `strict` (container / VM) | **Not implemented.** Always refuses to run | Nothing yet |

* **`process` mode refuses to run** unless the operator declares the host a trusted, disposable development
  environment (`--trusted-dev-env` on the command line, `isolation.trusted_environment: true` in the API).
  The declaration is a statement about the code and the machine. It does not make anything safer.
* **`strict` mode never falls back to `process` mode.** Requesting it ends with `isolation_unavailable`
  (command-line exit code 3) and executes nothing.
* Static validation of a candidate (`candidate validate`, `candidate register`) parses files and never
  imports or executes them. Passing validation does not make a candidate trustworthy.

### What `process` mode does not protect

Candidate code in `process` mode runs as the same operating-system user as the harness. It can:

* read any file that user can read, including the oracle, the scenarios, the reference implementations,
  other candidates and your own files;
* switch off GAUNTLET's Python audit-hook guard with a few lines of ordinary Python (no native code is
  needed), and bypass it entirely with native code;
* then open network connections, including a new connection to the database server, and write files
  outside its workspace;
* read the harness's environment and memory, which hold the PostgreSQL administrator credentials.

The audit-hook guard is a tripwire that stops trusted code from using sockets, processes and file writes
by accident. It is not a security control against a hostile candidate. The operating-system limits are
different: on Windows the job object keeps blocking child processes and enforcing the memory limit after
the guard has been switched off (verified by tests).

The controls that `process` mode does apply (allow-listed environment, per-actor workspace, job object or
process group, resource limits, audit hook, no database connection in the actor process, per-run database
roles) are defence in depth for trusted code. The Windows controls are exercised by tests. On POSIX the
sandboxed actor path runs in continuous integration, but the tests that check its resource limits are
skipped there, so those limits are unverified, and the process-count limit is not enforced at all.

## Database credentials

* The harness connects with an administrator DSN (`GAUNTLET_PG_DSN`). It needs it to create and drop a
  database and roles for every run, and for a control connection to the maintenance database.
* An external candidate's statements run as a per-run role that owns only its run database and is not a
  superuser. Its database is read by a separate per-run observer role, never by a superuser session.
* The actor process that hosts candidate code is given no connection, DSN or credentials. In `process` mode
  this is not a boundary, because the candidate shares an operating-system user with the harness that does
  hold them.
* The default local cluster uses the published development credentials `gauntlet` / `gauntlet` on
  `127.0.0.1:55432`, for a **superuser**. They are not secrets and are not suitable for production. Never
  use them for a server that is reachable from a network, and do not use them on a machine shared with
  other user accounts: anyone who can log in to the machine can connect to `127.0.0.1:55432` as that
  superuser. Point `GAUNTLET_PG_DSN` at a server with your own password instead.

## API and network assumptions

* The API (`python -m gauntlet serve`) has **no authentication**. It binds to `127.0.0.1` by default and
  must stay there. Anyone who can reach the port can start runs.
* The API refuses requests whose `Host` header is not a loopback name, state-changing requests from a
  foreign web origin, and request bodies that are not `application/json`. This stops a web page in the
  operator's browser from driving the API (DNS rebinding, cross-site requests). It is not authentication:
  any local process, and any other user account on the same machine, can still use the API.
* The dashboard's development server (`npm run dev`, vite) listens on `localhost` only, sends no CORS
  headers, serves only files inside `frontend/`, and refuses requests addressed to a host name that is not
  local. Its proxy passes the browser's real `Host` header to the API, so the API's own checks apply too.
  Earlier vite releases (the 5.x line pinned before this release) let any website the operator had open
  read local files and API responses through the development server. Keep the pinned vite version, and do
  not start the development server with `--host`.
* GAUNTLET applies no network isolation to candidate code. The only network boundary is one you provide.

## Recommendations for safe use

1. Run only code you wrote or trust on your own machine.
2. To evaluate untrusted code, use a disposable VM with the network adapter disabled and no shared
   folders, install GAUNTLET and PostgreSQL inside it, use a random database password, and discard the VM
   afterwards. This is how the one AI-generated candidate recorded in
   [research/gemini-attempt2-results.md](research/gemini-attempt2-results.md) was run.
3. Do not register untrusted candidates on a host where you also run the API or the dashboard.
4. Keep `GAUNTLET_RESULTS_DB` away from `evidence/`; work on copies of the frozen evidence.
5. Do not expose the API or the PostgreSQL port on a network interface.

## Supported versions

Only the latest release receives fixes. The benchmark records of v0.1.0 to v0.3.0 are kept unchanged as
research records.

## Reporting a vulnerability

Please report suspected vulnerabilities privately, not in a public issue. Use GitHub's private
vulnerability reporting for this repository: **Security → Report a vulnerability**. If that option is not
shown, private reporting has not been enabled; in that case open an issue that says only that you have a
security report and asks for a private channel, without any technical detail.

Include the version or commit, the platform, steps to reproduce, and the impact you expect. This is a
single-maintainer research project: you should receive an acknowledgement within 14 days, and response
times are best effort.

## Known limitations that are not new vulnerabilities

Reports that only restate the following are welcome as discussion, but they describe the documented design:

* `process` isolation is not a security boundary (see above).
* `strict` isolation is not implemented and fails closed.
* The harness holds PostgreSQL administrator credentials, which native candidate code can read in `process`
  mode.
* A candidate statement that exceeds the step timeout is attributed to the candidate
  (`candidate_statement_timeout`) only when the harness's evidence supports it; otherwise it remains an
  infrastructure error. The evidence rule is a heuristic. Neither outcome is ever a pass.
* The local API has no authentication.
* The default local PostgreSQL credentials are development defaults.
* Experiment evidence manifests detect accidental change, not deliberate tampering by someone who can
  rewrite the results database.
