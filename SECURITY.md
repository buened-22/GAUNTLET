# Security policy

GAUNTLET is a research prototype. It is not intended for production use and no execution mode in this
version is a security boundary for untrusted code. Read [docs/SECURITY_MODEL.md](docs/SECURITY_MODEL.md)
before running anything that you did not write yourself.

## Supported versions

Only the latest release receives fixes. Historical releases (`v0.1.0` to `v0.3.0`) are kept unchanged as
research records.

## Reporting a vulnerability

Please report suspected vulnerabilities privately, not in a public issue. Once the repository is hosted,
use the hosting platform's private vulnerability reporting (for example, GitHub Security Advisories:
*Security → Report a vulnerability*). Include the version or commit, the platform, steps to reproduce, and
the impact you expect.

You should receive an acknowledgement within 14 days. This is a single-maintainer research project, so
response times are best effort.

## Known, documented limitations (not vulnerabilities)

These are stated limitations of the current design. Reports that only restate them are welcome as
discussion but are not treated as new vulnerabilities:

* `process` isolation does not prevent candidate code from reading host files (including the oracle),
  using native code to open network connections or bypassing the Python audit-hook guard.
* `strict` (container / VM) isolation is not implemented and fails closed.
* The local API has no authentication and must only listen on `127.0.0.1`.
* The default local PostgreSQL credentials (`gauntlet` / `gauntlet`) are development defaults.
