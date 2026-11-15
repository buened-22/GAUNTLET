# Repository history and provenance

The public history of this repository has two commits, one per release:

| Commit | Tag | Contents |
|---|---|---|
| `be9b742` | `v0.4.0` | The v0.4.0 release, 2026-10-22 |
| the commit tagged `v0.4.1` | `v0.4.1` | The v0.4.1 release: everything developed after v0.4.0, as one commit |

The work behind each release was done elsewhere and its commit-by-commit history is **not** included:

* GAUNTLET v0.1.0 to v0.3.0, and the v0.4.0 changes before that release, were developed in a separate,
  unpublished development repository. This repository starts with the v0.4.0 release commit.
* v0.4.1 was developed in 22 commits on a private development branch (`gauntlet/full-audit-hardening`),
  kept in a private archive. Its final tree is the v0.4.1 release commit, file for file.

Nothing recorded was changed by the move. The frozen evidence (`evidence/`), the historical results
records (`docs/RESULTS.md`, `docs/RESULTS-v0.2.0.md`, `docs/RESULTS-v0.3.0.md`), the v0.1.0 self-audit
(`docs/GAUNTLET_VALIDATION_AUDIT.md`) and the audit scripts (`audit/`) are byte-for-byte identical to the
files in the development repository. Their SHA-256 checksums are in `evidence/*.sha256`. The oracle,
scenario catalog, references and mutants are also unchanged.

## Original development-repository identifiers

These identifiers are preserved exactly. Run records in the evidence databases and in captured demo
output refer to them as their "git revision".

| Original commit | Date | Subject |
|---|---|---|
| `f49424ffdfa24e9734680a6b969d66e42823cd55` | 2026-10-18 | chore: establish reproducible GAUNTLET baseline |
| `3102b6b2d1a3f5f606f627a335f1872c4b0b00ab` | 2026-10-18 | fix: strengthen oracle identity and seeded schedule diversity |
| `694577b709484b7ff274f049e470da5d9854ec45` | 2026-10-19 | release: GAUNTLET v0.3.0 |

The v0.3.0 release was the annotated tag `v0.3.0` (tag object
`24f9494c02feabd4b7e22ed61dc012e9540bee64`), pointing to commit `694577b709484b7ff274f049e470da5d9854ec45`.
That tag exists only in the development repository. The v0.4.0 working tree was validated on top of that
commit before it was copied here (see [RELEASE_v0.4.0.md](RELEASE_v0.4.0.md)).

## Why v0.4.1 is a single commit

Two reasons, stated plainly:

* **Licensing.** Several of the intermediate commits contained a short fragment adapted from psycopg, the
  PostgreSQL driver, which is licensed under LGPL-3.0. The fragment was never part of a release and was
  replaced before v0.4.1 by code written for this project against psycopg's public interfaces
  ([THIRD_PARTY.md](THIRD_PARTY.md)). Publishing the intermediate commits would have published that
  fragment in a repository presented as Apache-2.0.
* **Relevance.** The intermediate commits also carry working notes that are not part of the project.

Nothing about the release itself is hidden by this. What changed between v0.4.0 and v0.4.1 is listed in
[../CHANGELOG.md](../CHANGELOG.md) and [RELEASE_v0.4.1.md](RELEASE_v0.4.1.md), and `git diff v0.4.0 v0.4.1`
shows every changed line.

### Development-branch identifiers cited in the documentation

Documents written during the v0.4.1 work cite commits of the private development branch. Those commits
do not exist in this repository, so the identifiers cannot be resolved here; they are kept because the
recorded results refer to them.

| Identifier | Date | What it was |
|---|---|---|
| `7c3ac9f` | 2026-11-05 | Harness-owned database connections (audit finding A-01) |
| `58f628e` | 2026-11-05 | Evidence-based classification of statement timeouts (FR-3) |
| `2a73551` | 2026-11-05 | Non-superuser observer role (A-02); the build used for the rehearsed demonstration |
| `b4d4ef6` | 2026-11-06 | Isolation record in evaluation summaries |
| `07ab2ff` | 2026-11-06 | Demonstration documents; the harness revision of the 2026-11-07 candidate evaluation |
| `2f3ae4e` | 2026-11-13 | Last commit that changed code before v0.4.1; the continuous-integration figures in the release notes |

Run records produced during that work store these development revisions as their "git revision", in the
same way that the frozen benchmark evidence stores revisions of the original development repository.

## Verifying the frozen evidence without the old history

The git tag cannot be checked in this repository. The evidence is verified by its checksums instead:

```bash
(cd evidence && sha256sum -c *.sha256)   # four OK lines
```

`evidence/results-bench-eed0c47427.sqlite3` is the v0.3.0 benchmark. Each of its run records stores the
development-repository revision it was produced from. Run records produced from a clone of this repository
store this repository's revision instead.
