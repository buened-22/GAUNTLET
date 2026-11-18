# Archive: working records from the v0.4.1 development

These documents were written while v0.4.1 was being developed, audited and rehearsed. They are kept
for provenance and are **not** current documentation. For the current state, start at
[../README.md](../README.md).

They were moved here unchanged in content (the bytes Git stores are identical to the v0.4.1 release
commit; only the path differs). Nothing in them was edited, so:

* their findings, dates and statuses describe the time they were written, not the current release;
* relative links inside them still use their **original** relative paths. Short navigation pointer pages
  at those paths in this archive (for example `docs/SECURITY_MODEL.md` here) lead to the current
  documents, so the links resolve without the records being edited. The same files at their original
  paths are in the tagged release `v0.4.1`;
* commit identifiers such as `2a73551` or `07ab2ff` belong to the private development branch and cannot be
  resolved in this repository ([../HISTORY.md](../HISTORY.md) explains them).

**Authorship.** These are self-audits and working notes from the author's own development work, which used
an AI coding assistant; the readiness audit says on its first lines that its auditor was an AI assistant
acting for the author. None of these documents is an independent review, and none was reviewed by a third
party.

| Document | Original path | Written | What it is | Superseded by |
|---|---|---|---|---|
| [docs/FINAL_RELEASE_READINESS_AUDIT.md](docs/FINAL_RELEASE_READINESS_AUDIT.md) | `docs/` | 2026-11-05 | Release-readiness audit of the hardening branch: findings FR-1 to FR-16 and their follow-ups | Current status: [../SECURITY_FINDINGS.md](../SECURITY_FINDINGS.md), [../SECURITY_MODEL.md](../SECURITY_MODEL.md), [../LIMITATIONS.md](../LIMITATIONS.md) |
| [docs/A02_TIMEOUT_REMEDIATION_PLAN.md](docs/A02_TIMEOUT_REMEDIATION_PLAN.md) | `docs/` | 2026-11-05 | Investigation of, and plan for, findings A-02 and FR-3 | Implemented in v0.4.1; behaviour described in [../SECURITY_MODEL.md](../SECURITY_MODEL.md) |
| [docs/DEMO_SECURITY_NOTES.md](docs/DEMO_SECURITY_NOTES.md) | `docs/` | 2026-11 | Presenter-facing security summary for the demonstration, pinned to development commit `2a73551` | [../SECURITY_MODEL.md](../SECURITY_MODEL.md), [../../SECURITY.md](../../SECURITY.md) |
| [research/demo-readiness-report.md](research/demo-readiness-report.md) | `research/` | 2026-11-05 | Rehearsal of the demonstration at development commit `2a73551` | [../DEMO.md](../DEMO.md) (current runbook) |
| [research/demo-script.md](research/demo-script.md) | `research/` | 2026-11 | Script for a recorded five-minute video, including the Gemini demonstration clips | [../DEMO.md](../DEMO.md) |
| [research/demo-transcript.md](research/demo-transcript.md) | `research/` | 2026-10-22 | Captured demonstration output from the v0.4.0 release review | [../DEMO.md](../DEMO.md) |

Records that stay at their original paths because other records and checks refer to them, and that are
never edited: the v0.1.0 self-audit [../GAUNTLET_VALIDATION_AUDIT.md](../GAUNTLET_VALIDATION_AUDIT.md), the
audit scripts in [../../audit/](../../audit/), the generated results records `docs/RESULTS*.md` and the
frozen evidence in [../../evidence/](../../evidence/).
