"""End-to-end evaluation of a registered external candidate with GAUNTLET's existing engines.

Workflow (each step reuses an existing module; nothing here judges correctness itself):

1. verify the candidate snapshot (content hash) and the isolation mode (fail closed);
2. for every selected (scenario, seed): ``differential.run_differential`` over the references and the
   candidate on one schedule document (each on its own database). These are the candidate's benchmark
   runs; the reference runs are its matched controls;
3. optionally ``explore.explore`` the candidate on the exploration fixture within the given bounds;
4. optionally ``minimize.minimize`` reproducible candidate failures (one per failing scenario);
5. ``experiment`` report over all of these runs with a stopping rule (references + candidate x scenarios
   x seeds) and a check of its evidence manifest;
6. a summary that keeps observations separate from interpretation.

Every step is persisted; nothing overwrites an earlier evaluation. If a step cannot run, the evaluation
records it as incomplete or blocked instead of producing a result.
"""

from __future__ import annotations

import time
from datetime import datetime, timezone
from typing import Callable

from gauntlet import BENCHMARK_VERSION
from gauntlet.candidates import LOADER_VERSION, impl_id_for
from gauntlet.candidates.sandbox import IsolationConfig, IsolationUnavailable, capabilities, check
from gauntlet.candidates.snapshots import CandidateError, verify
from gauntlet.differential import run_differential
from gauntlet.execution import outcome_class
from gauntlet.experiment import generate_report, new_experiment, verify_manifest
from gauntlet.explore import explore
from gauntlet.impls.registry import IMPLEMENTATIONS
from gauntlet.minimize import minimize
from gauntlet.schedules import implementation_fingerprint, resolve_scenario
from gauntlet.store import Store

DEFAULTS = {
    "scenarios": ["S01_baseline_migration", "S02_backfill_vs_legacy_writes", "S03_lost_ack_and_retry",
                  "S04_crash_and_resume", "S05_premature_finalization", "S06_unbackfilled_records"],
    "seeds": [1],
    "references": ["ref_a_trigger", "ref_b_dualwrite"],
    "run_time_limit_s": 180,
    "explore": {"scenario_id": "F01_backfill_race_probe", "seed": 1, "max_schedules": 40, "max_preemptions": 1},
    "minimize": {"max_attempts": 40, "max_failures": 2},
}


def evaluate(store: Store, candidate_id: str, *, scenarios: list[str] | None = None, seeds: list[int] | None = None,
             references: list[str] | None = None, isolation: dict | None = None, run_time_limit_s: float | None = None,
             explore_cfg: dict | None | bool = None, minimize_cfg: dict | None | bool = None,
             evaluation_id: str | None = None, cancel: Callable[[], bool] | None = None) -> dict:
    scenarios = scenarios or DEFAULTS["scenarios"]
    seeds = seeds or DEFAULTS["seeds"]
    references = references or DEFAULTS["references"]
    limit = run_time_limit_s or DEFAULTS["run_time_limit_s"]
    exp_cfg = None if explore_cfg is False else {**DEFAULTS["explore"], **(explore_cfg or {})}
    min_cfg = None if minimize_cfg is False else {**DEFAULTS["minimize"], **(minimize_cfg or {})}
    impl = impl_id_for(candidate_id)
    for r in references:
        if r not in IMPLEMENTATIONS or IMPLEMENTATIONS[r].kind != "reference":
            raise ValueError(f"{r!r} is not a reference implementation")
    for s in scenarios:
        resolve_scenario(s)
    config = {"candidate_id": candidate_id, "impl_id": impl, "scenarios": scenarios, "seeds": seeds,
              "references": references, "isolation": isolation or {}, "run_time_limit_s": limit,
              "explore": exp_cfg, "minimize": min_cfg, "benchmark_version": BENCHMARK_VERSION,
              "loader_version": LOADER_VERSION}
    eid = evaluation_id or store.create_analysis("evaluation", config, status="running")
    store.update_analysis(eid, status="running", config=config)
    started = datetime.now(timezone.utc).isoformat()
    t0 = time.monotonic()
    res: dict = {"started_at": started, "steps": {}, "notes": []}

    def persist(status: str):
        res["elapsed_s"] = round(time.monotonic() - t0, 1)
        store.update_analysis(eid, status=status, result=res)

    def stopped() -> bool:
        return bool(cancel and cancel())

    # 1. identity and isolation (fail closed)
    try:
        meta = verify(candidate_id)
        res["candidate"] = {k: meta[k] for k in ("candidate_id", "source_hash", "contract_version", "loader_version",
                                                  "registered_at", "source_dir_name", "files", "excluded")}
        res["candidate"]["manifest"] = meta["manifest"]
        cfg_obj = IsolationConfig.from_dict(isolation)
        res["isolation"] = check(cfg_obj)
        iso_cfg = cfg_obj.to_dict()  # the exact configuration every candidate run below executes with
        res["isolation_capabilities"] = capabilities()
    except (CandidateError, IsolationUnavailable, ValueError) as exc:
        res["blocked"] = f"{type(exc).__name__}: {exc}"
        res["finished_at"] = datetime.now(timezone.utc).isoformat()
        persist("blocked")
        return {"id": eid, "config": config, "result": res}
    res["references"] = {r: implementation_fingerprint(r) for r in references}
    res["harness"] = harness_provenance()
    res["agent_task_bundle"] = agent_task_bundle()

    # 2. benchmark runs as differential comparisons (references + candidate on the same schedule)
    comparisons = []
    for sc in scenarios:
        for seed in seeds:
            if stopped():
                break
            c = run_differential(store, references + [impl], sc, seed, time_limit_s=limit, isolation=iso_cfg)
            run_of = {r["impl_id"]: r for r in c["result"]["runs"]}
            cand = run_of[impl]
            comparisons.append({"id": c["id"], "scenario_id": sc, "seed": seed,
                                "classification": c["result"]["classification"],
                                "candidate_run": cand["id"], "candidate_outcome": outcome_class(cand),
                                "candidate_failed_invariants": cand["failed_invariants"],
                                "reference_outcomes": {r: outcome_class(run_of[r]) for r in references},
                                "schedule_id": c["config"]["schedule_id"]})
            res["steps"]["benchmark"] = {"comparisons": comparisons}
            persist("running")
    expected = len(scenarios) * len(seeds)
    res["steps"]["benchmark"] = {"comparisons": comparisons, "expected": expected, "completed": len(comparisons)}

    # 3. bounded exploration of the candidate
    if exp_cfg and not stopped():
        x = explore(store, exp_cfg["scenario_id"], impl, exp_cfg["seed"],
                    limits={"max_schedules": exp_cfg["max_schedules"], "max_preemptions": exp_cfg["max_preemptions"],
                            "run_time_limit_s": limit}, cancel=cancel, isolation=iso_cfg)
        xr = x["result"]
        res["steps"]["exploration"] = {"id": x["id"], "status": xr["status"], "stop_reason": xr["stop_reason"],
                                       "schedules_executed": xr["schedules_executed"], "outcomes": xr["outcomes"],
                                       "exhaustive_within_bounds": xr["exhaustive_within_bounds"],
                                       "exhaustive_claim": xr["exhaustive_claim"], "unexplored_known": xr["unexplored_known"],
                                       "failure_classes": [{k: c.get(k) for k in ("id", "failed_invariants", "diagnosis_categories",
                                                                                  "count", "replay", "flaky")}
                                                           for c in xr["failure_classes"]],
                                       "bounds": {"scenario_id": exp_cfg["scenario_id"], "max_preemptions": exp_cfg["max_preemptions"],
                                                  "max_schedules": exp_cfg["max_schedules"]}}
        persist("running")
    elif exp_cfg is None:
        res["steps"]["exploration"] = {"status": "not requested"}

    # 4. minimization of reproducible candidate failures (one per failing scenario)
    if min_cfg and not stopped():
        mins = []
        failing = [c for c in comparisons if c["candidate_outcome"] == "fail"]
        seen = set()
        for c in failing:
            if c["scenario_id"] in seen or len(mins) >= min_cfg["max_failures"] or stopped():
                continue
            seen.add(c["scenario_id"])
            m = minimize(store, c["candidate_run"], budget={"max_attempts": min_cfg["max_attempts"],
                                                            "run_time_limit_s": limit}, cancel=cancel)
            mr = m["result"]
            mins.append({"id": m["id"], "run_id": c["candidate_run"], "scenario_id": c["scenario_id"],
                         "invariant": m["config"]["invariant"], "status": mr["status"], "stop_reason": mr.get("stop_reason"),
                         "minimality": mr.get("minimality"), "reduction": mr.get("reduction"),
                         "replay": (mr.get("minimized") or {}).get("replay"),
                         "original_unchanged": mr.get("original_unchanged")})
        res["steps"]["minimization"] = {"minimizations": mins, "candidate_failures": len(failing)}
        persist("running")
    elif min_cfg is None:
        res["steps"]["minimization"] = {"status": "not requested"}

    # 5. experiment report over the runs (existing reporting pipeline) and evidence-manifest check
    run_ids = sorted({r for c in comparisons for r in _runs_of(store, c["id"])})
    analysis_ids = [res["steps"]["exploration"]["id"]] if res["steps"].get("exploration", {}).get("id") else []
    if run_ids:
        xid = new_experiment(store, {
            "title": f"External candidate {res['candidate']['manifest']['name']} ({candidate_id})",
            "experiment_type": "exploratory",
            "research_question": "Does this external candidate violate GAUNTLET's safety or recovery invariants under the "
                                 "benchmark's adversarial schedules, where the references do not?",
            "hypotheses": [], "selection": {"run_ids": run_ids, "analysis_ids": analysis_ids},
            "required": {"impls": references + [impl], "scenarios": scenarios, "seeds": seeds},
            "paired_comparisons": [[r, impl] for r in references], "sampling": "fixed_set",
            # A manifest's producer is a claim, not an evaluated agent: it is never listed as one.
            "agents": [], "candidate_producer_claimed": res["candidate"]["manifest"].get("producer")})
        rep = generate_report(store, xid)
        check_ = verify_manifest(store, rep["manifest"])
        res["steps"]["report"] = {"experiment_id": xid, "markdown_sha256": rep["markdown_sha256"],
                                  "completeness": rep["results"]["completeness"], "manifest_verification": check_,
                                  "paired": rep["results"]["paired"]}
    else:
        res["steps"]["report"] = {"status": "not generated: no runs completed"}

    step_failures = [f"{k}: {s['status']}" for k, s in (("exploration", res["steps"].get("exploration", {})),)
                     if s.get("status") in ("error", "interrupted")]
    step_failures += [f"minimization {m['id']}: {m['status']}" for m in
                      res["steps"].get("minimization", {}).get("minimizations", []) if m["status"] in ("error", "interrupted")]
    res["step_failures"] = step_failures
    complete = (len(comparisons) == expected and not stopped() and not step_failures
                and res["steps"]["report"].get("completeness", {}).get("status") == "complete"
                and res["steps"]["report"].get("manifest_verification", {}).get("ok"))
    status = "interrupted" if stopped() else ("completed" if complete else "incomplete")
    res["finished_at"] = datetime.now(timezone.utc).isoformat()
    res["summary_markdown"] = summary(eid, config, res, status)
    persist(status)
    return {"id": eid, "config": config, "result": res, "status": status}


AGENT_TASK_FILES = ("docs/agent_task/TASK.md", "backend/gauntlet/impls/base.py", "backend/gauntlet/impls/legacy.py",
                    "backend/gauntlet/money.py")


def agent_task_bundle() -> dict:
    """SHA-256 of the files the agent-evaluation protocol gives to an agent (as they are now). Recorded so a
    study can show which task text and contract an evaluated candidate was measured against."""
    import hashlib
    from gauntlet.config import REPO_ROOT
    files = {}
    for rel in AGENT_TASK_FILES:
        p = REPO_ROOT / rel
        files[rel] = hashlib.sha256(p.read_bytes().replace(b"\r\n", b"\n")).hexdigest() if p.is_file() else None
    return {"files": files, "note": "hashes of the current task bundle (LF-normalized); the prompt actually given to "
                                    "an agent must be recorded separately by the operator"}


def harness_provenance() -> dict:
    import platform
    import sys
    from importlib import metadata
    from gauntlet.pg.provision import InfrastructureError, check_server
    from gauntlet.schedules import engine_fingerprint, git_revision, harness_source, oracle_fingerprint
    try:
        postgres = check_server()["server_version"]
    except InfrastructureError as exc:
        postgres = f"unavailable ({exc})"
    src = harness_source()
    pkgs = {}
    for name in ("psycopg", "psycopg-binary", "fastapi", "pydantic", "uvicorn"):
        try:
            pkgs[name] = metadata.version(name)
        except metadata.PackageNotFoundError:
            pkgs[name] = None
    return {"git": git_revision(), "benchmark_version": BENCHMARK_VERSION, "oracle_fingerprint": oracle_fingerprint(),
            "engine_fingerprint": engine_fingerprint(), "source_fingerprint": src["fingerprint"],
            "source_changed_since_process_start": src["changed_since_process_start"], "postgres": postgres,
            "python": sys.version.split()[0], "platform": platform.platform(), "packages": pkgs}


def _runs_of(store: Store, comparison_id: str) -> list[str]:
    a = store.get_analysis(comparison_id)
    return [r["id"] for r in (a["result"] or {}).get("runs", [])]


def summary(eid: str, cfg: dict, res: dict, status: str) -> str:
    c = res["candidate"]
    m = c["manifest"]
    bench = res["steps"]["benchmark"]
    L = [f"# Candidate evaluation `{eid}`", "",
         f"Status: **{status}**. Generated from persisted GAUNTLET records; observations and interpretation are "
         "kept separate.", "",
         "## Candidate", "",
         f"* Name `{m['name']}`, candidate id `{c['candidate_id']}`, strategy {m['strategy']}.",
         f"* Source hash (canonical, SHA-256): `{c['source_hash']}`; contract {c['contract_version']}; loader {c['loader_version']}.",
         f"* Producer (as claimed in the manifest, not verified): `{m.get('producer') or 'not stated'}`.",
         f"* Files ({len(c['files'])}): " + ", ".join(f"`{f['path']}`" for f in c["files"]), "",
         "## Harness and task provenance", "",
         f"* Harness git revision `{res['harness']['git'].get('head')}` (backend modified relative to it: "
         f"{res['harness']['git'].get('backend_modified')}); harness source fingerprint "
         f"`{res['harness']['source_fingerprint'][:16]}` (identifies uncommitted changes; changed since the evaluating "
         f"process started: {res['harness']['source_changed_since_process_start']}); oracle "
         f"`{res['harness']['oracle_fingerprint'][:16]}`; engine `{res['harness']['engine_fingerprint'][:16]}`; "
         f"PostgreSQL {res['harness']['postgres']}; Python {res['harness']['python']}; packages {res['harness']['packages']}.",
         "* Agent task bundle (current hashes): " + ", ".join(f"`{k}` {(v or 'missing')[:16]}"
                                                         for k, v in res["agent_task_bundle"]["files"].items()) + ".",
         "* Not recorded by GAUNTLET: the prompt actually given to an agent, the agent's tool and network settings, and the "
         "attempt number; these belong in the operator's study log (see docs/AGENT_EVALUATION_PROTOCOL.md).", "",
         "## Isolation", "",
         f"* Mode `{res['isolation']['mode']}`. {res['isolation']['warning']}.",
         "* Applied: " + "; ".join(res["isolation"]["controls"]) + ".",
         "* Not enforced: " + "; ".join(res["isolation"]["not_enforced"]) + ".",
         f"* Strict (container) isolation on this host: {res['isolation_capabilities']['strict']['reason']}.", "",
         "## Benchmark configuration", "",
         f"* Benchmark version {cfg['benchmark_version']}; scenarios {cfg['scenarios']}; seeds {cfg['seeds']}; "
         f"references {cfg['references']}; per-run limit {cfg['run_time_limit_s']} s.",
         "* Fair comparison: for each (scenario, seed) every implementation executed the identical schedule document on its "
         "own freshly created database with the same initial data; references used the harness role, the candidate a "
         "restricted per-run role and sandboxed actor processes (this difference in execution conditions is intentional).", "",
         "## Observations", "",
         f"* Benchmark comparisons completed: {bench['completed']} of {bench['expected']}.", "",
         "| Scenario | Seed | Candidate outcome | Candidate FAILed invariants | References | Differential classification |",
         "|---|---|---|---|---|---|"]
    for x in bench["comparisons"]:
        L.append(f"| {x['scenario_id']} | {x['seed']} | {x['candidate_outcome']} | {', '.join(x['candidate_failed_invariants']) or '-'} "
                 f"| {', '.join(f'{k}: {v}' for k, v in x['reference_outcomes'].items())} | {x['classification']} |")
    infra = [x for x in bench["comparisons"] if x["candidate_outcome"] not in ("pass", "fail")]
    L += ["", f"* Candidate runs that did not reach a verdict (infrastructure, timeout, candidate execution failure, "
              f"inconclusive): {len(infra)}" + (": " + ", ".join(f"{x['scenario_id']}/{x['seed']} {x['candidate_outcome']}"
                                                                for x in infra) if infra else "") + "."]
    ex = res["steps"].get("exploration", {})
    if ex.get("id"):
        L += ["", "### Exploration", "",
              f"* `{ex['id']}` on {ex['bounds']['scenario_id']}: {ex['schedules_executed']} schedules, status {ex['status']}"
              f"{' (' + ex['stop_reason'] + ')' if ex['stop_reason'] else ''}; outcomes {ex['outcomes']}.",
              f"* Coverage: {ex['exhaustive_claim'] or 'not exhaustive within the bounds'}; known unexplored schedules: "
              f"{ex['unexplored_known']}. Bounds: at most {ex['bounds']['max_preemptions']} preemption(s), "
              f"{ex['bounds']['max_schedules']} schedules, one interleave directive; no crash-point exploration."]
        for fc in ex["failure_classes"]:
            L.append(f"* Failure class `{fc['id']}`: {fc['failed_invariants']} ({fc['diagnosis_categories']}), "
                     f"{fc['count']} schedule(s); replay: {fc.get('replay')}.")
    mn = res["steps"].get("minimization", {})
    if mn.get("minimizations") is not None:
        L += ["", "### Minimization", "", f"* Candidate failing runs: {mn['candidate_failures']}; minimized: {len(mn['minimizations'])}."]
        for x in mn["minimizations"]:
            L.append(f"* `{x['id']}` ({x['scenario_id']}, {x['invariant']}): {x['status']}; {x['minimality']}; reduction "
                     f"{x['reduction']}; independent replay {x['replay']}; original unchanged {x['original_unchanged']}.")
    rp = res["steps"]["report"]
    L += ["", "### Report and evidence", ""]
    if rp.get("experiment_id"):
        L += [f"* Experiment report `{rp['experiment_id']}` (markdown SHA-256 `{rp['markdown_sha256'][:16]}`); completeness "
              f"{rp['completeness']['status']}; evidence-manifest verification: "
              f"{'all ' + str(rp['manifest_verification']['checked']) + ' artifacts present and unchanged' if rp['manifest_verification']['ok'] else rp['manifest_verification']}."]
        for p in rp["paired"]:
            L.append(f"* Paired with `{p['a']}` (matched scenario and seed): both pass {p['both_pass']}, only reference fails "
                     f"{p['only_a_fails']}, only candidate fails {p['only_b_fails']}, both fail {p['both_fail']}, "
                     f"unmatched / non-conclusive {p['unmatched_or_non_conclusive']}.")
    else:
        L.append(f"* {rp.get('status')}")
    if res.get("step_failures"):
        L += ["", f"* Steps that did not finish: {', '.join(res['step_failures'])} (the evaluation is therefore incomplete)."]
    L += ["", "## Interpretation (not observations)", "",
          "* A FAIL is a confirmed invariant violation observed under these schedules; the oracle's diagnosis states what "
          "the evidence is consistent with, not a proven root cause.",
          "* Passing every scenario is evidence about these schedules only; it does not show the candidate is correct.",
          "* One candidate is one sample: nothing here estimates how often an agent or model produces correct migrations.", "",
          "## Reproduction", "",
          f"1. Verify the snapshot: `python -m gauntlet candidate verify {c['candidate_id']}` (hash `{c['source_hash'][:16]}…`).",
          f"2. Re-run: `python -m gauntlet candidate evaluate --id {c['candidate_id']} --scenarios {','.join(cfg['scenarios'])} "
          f"--seeds {','.join(map(str, cfg['seeds']))}`.",
          "3. Replay any run: `python -m gauntlet replay <run_id>` (logical hashes, not timing, are compared)."]
    return "\n".join(L) + "\n"
