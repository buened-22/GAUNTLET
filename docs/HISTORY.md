# Repository history and provenance

This repository was created fresh for the v0.4.0 release. GAUNTLET v0.1.0 to v0.3.0, and the v0.4.0
changes before this release, were developed in a separate, unpublished development repository. Its
history is **not** included here; this repository starts with the v0.4.0 release commit.

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

## Verifying the frozen evidence without the old history

The git tag cannot be checked in this repository. The evidence is verified by its checksums instead:

```bash
(cd evidence && sha256sum -c *.sha256)   # four OK lines
```

`evidence/results-bench-eed0c47427.sqlite3` is the v0.3.0 benchmark. Each of its run records stores the
development-repository revision it was produced from. Run records produced in this repository store this
repository's revision instead.
