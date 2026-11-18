# GAUNTLET documentation

Start with the project [README](../README.md). This page lists every document by what you want to do.

## Run it

| Document | Contents |
|---|---|
| [GETTING_STARTED.md](GETTING_STARTED.md) | Requirements and platforms, installation, PostgreSQL 16, first run, commands, configuration, dashboard |
| [DEMO.md](DEMO.md) | Step-by-step demonstration runbook |
| [API.md](API.md) | The local HTTP API: every endpoint, request checks, execution model |
| [CANDIDATES.md](CANDIDATES.md) | Evaluating an implementation produced outside GAUNTLET: contract, isolation modes, workflow |
| [TESTING.md](TESTING.md) | Running the test suite, current results, continuous integration, known flakiness |

## Understand how it works

| Document | Contents |
|---|---|
| [ARCHITECTURE.md](ARCHITECTURE.md) | Components, module boundaries, how a run works, design trade-offs, repository layout |
| [INVARIANTS.md](INVARIANTS.md) | The oracle's 12 invariants and outcome terms |
| [FAULT_CATALOG.md](FAULT_CATALOG.md) | Faults and the eight hand-written mutants |
| [ANALYSES.md](ANALYSES.md) | Differential testing, schedule exploration, failure minimization, experiment reports |
| [agent_task/TASK.md](agent_task/TASK.md) | The task bundle given to an external implementer (its hash is recorded in every evaluation) |
| [../PROJECT_SPEC.md](../PROJECT_SPEC.md) | The original specification and its implementation status |

## Security and limitations

| Document | Contents |
|---|---|
| [../SECURITY.md](../SECURITY.md) | Security policy, execution modes in brief, safe-use recommendations, how to report a vulnerability |
| [SECURITY_MODEL.md](SECURITY_MODEL.md) | Threat model; what `process` and `strict` modes do and do not guarantee; the API and dashboard |
| [SECURITY_FINDINGS.md](SECURITY_FINDINGS.md) | Where each audit finding identifier comes from and where its status is recorded |
| [LIMITATIONS.md](LIMITATIONS.md) | What the prototype does not show; known technical weaknesses |

## Research, results and evidence

| Document | Contents |
|---|---|
| [RESEARCH_METHODOLOGY.md](RESEARCH_METHODOLOGY.md) | Research question, method, threats to validity |
| [RESEARCH_DESIGN.md](RESEARCH_DESIGN.md) | Hypotheses, variables, baselines, related work to review |
| [AGENT_EVALUATION_PROTOCOL.md](AGENT_EVALUATION_PROTOCOL.md) | The protocol for a future study of AI coding agents (not yet executed) |
| [RESULTS_OVERVIEW.md](RESULTS_OVERVIEW.md) | The three frozen benchmarks compared, and how to read them |
| [RESULTS-v0.3.0.md](RESULTS-v0.3.0.md), [RESULTS-v0.2.0.md](RESULTS-v0.2.0.md), [RESULTS.md](RESULTS.md) | Generated benchmark records for v0.3.0, v0.2.0 and v0.1.0 (frozen; `RESULTS.md` is v0.1.0) |
| [../evidence/README.md](../evidence/README.md) | The frozen benchmark databases and their checksums |
| [REPRODUCIBILITY.md](REPRODUCIBILITY.md) | Clean-checkout reproduction of the recorded results |
| [../research/technical-report.md](../research/technical-report.md) | Technical report |
| [../research/results-summary.md](../research/results-summary.md) | Everything measured so far, by outcome type |
| [../research/gemini-attempt2-results.md](../research/gemini-attempt2-results.md) | The one evaluation of AI-generated code (a demonstration, not a study) |
| [../research/open-questions.md](../research/open-questions.md) | Open questions, next milestones and roadmap |

## Project records

| Document | Contents |
|---|---|
| [../CHANGELOG.md](../CHANGELOG.md) | Changes per release |
| [RELEASE_v0.4.1.md](RELEASE_v0.4.1.md), [RELEASE_v0.4.0.md](RELEASE_v0.4.0.md) | Release notes |
| [HISTORY.md](HISTORY.md) | Why v0.4.0 and v0.4.1 are each a single commit; development identifiers cited in the documentation |
| [GAUNTLET_VALIDATION_AUDIT.md](GAUNTLET_VALIDATION_AUDIT.md) | The author's self-audit of v0.1.0 (frozen) |
| [../audit/PROVENANCE.md](../audit/PROVENANCE.md) | Provenance of the scripts used by that audit (frozen) |
| [archive/README.md](archive/README.md) | Working records from the v0.4.1 development: release-readiness audit, remediation plan, demonstration material |
| [THIRD_PARTY.md](THIRD_PARTY.md), [../NOTICE](../NOTICE), [../LICENSE](../LICENSE) | Dependencies and their licences; copyright notice; Apache License 2.0 |
| [../CONTRIBUTING.md](../CONTRIBUTING.md), [../CODE_OF_CONDUCT.md](../CODE_OF_CONDUCT.md), [../CITATION.cff](../CITATION.cff) | Contributing, code of conduct, citation |
