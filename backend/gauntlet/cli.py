"""Command-line interface: ``python -m gauntlet <command>``."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from gauntlet.config import REPO_ROOT


def _print_run(r: dict) -> None:
    print(f"run {r['id']}  {r['impl_id']} x {r['scenario_id']} seed={r['seed']}  verdict={r['verdict'].upper()}  "
          f"({r['duration_ms'] / 1000:.1f}s)")
    if r.get("infra_error"):
        print(f"  infrastructure error: {r['infra_error']}")
    for x in r.get("inconclusive", []):
        print(f"  inconclusive: {x}")
    for inv in r["invariants"]:
        print(f"  {inv['id']:<24} {inv['status']:<15} {inv['summary'][:110]}")
    print(f"  schedule={r['schedule_hash'][:16]} trace={(r['trace_hash'] or '-')[:16]} outcome={r['outcome_hash'][:16]}")


def _candidate(a, store) -> int:
    from gauntlet.candidates import snapshots
    from gauntlet.candidates.contract import validate
    from gauntlet.candidates.sandbox import capabilities
    if a.action == "isolation":
        print(json.dumps(capabilities(), indent=1))
        return 0
    if a.action == "list":
        for c in snapshots.list_candidates():
            print(f"{c['candidate_id']}  {c['name']:<28} strategy={c['strategy']} source={c['source_hash'][:16]}  "
                  f"registered {c['registered_at'][:19]}")
        return 0
    if a.action == "validate":
        v = validate(Path(a.target))
        print(json.dumps({k: v.to_dict()[k] for k in ("ok", "problems", "source_hash", "excluded")}, indent=1))
        return 0 if v.ok else 2
    if a.action == "register":
        try:
            meta = snapshots.register(Path(a.target))
        except snapshots.CandidateError as exc:
            print(str(exc), file=sys.stderr)
            return 2
        print(f"{'already registered' if meta['already_registered'] else 'registered'} {meta['candidate_id']} "
              f"(impl id {meta['impl_id']}, source hash {meta['source_hash']})")
        return 0
    if a.action == "verify":
        try:
            meta = snapshots.verify(a.target)
        except snapshots.CandidateError as exc:
            print(f"FAILED: {exc}", file=sys.stderr)
            return 2
        print(f"ok: {meta['candidate_id']} matches its recorded source hash {meta['source_hash']}")
        return 0
    # evaluate
    from gauntlet.candidates.evaluate import evaluate
    cid = a.candidate_id
    if a.candidate_dir:
        try:
            cid = snapshots.register(Path(a.candidate_dir))["candidate_id"]
        except snapshots.CandidateError as exc:
            print(str(exc), file=sys.stderr)
            return 2
    if not cid:
        print("evaluate needs --id or --dir", file=sys.stderr)
        return 2
    iso = {"mode": a.isolation, "trusted_environment": a.trusted_dev_env}
    if a.memory_mb:
        iso["memory_mb"] = a.memory_mb
    if a.cpu_seconds:
        iso["cpu_seconds"] = a.cpu_seconds
    out = evaluate(store, cid, scenarios=a.scenarios.split(",") if a.scenarios else None,
                   seeds=[int(x) for x in a.seeds.split(",")], references=a.refs.split(",") if a.refs else None,
                   isolation=iso, run_time_limit_s=a.time_limit,
                   explore_cfg=False if a.no_explore else {"max_schedules": a.explore_max_schedules,
                                                            "max_preemptions": a.explore_max_preemptions},
                   minimize_cfg=False if a.no_minimize else {"max_attempts": a.minimize_budget})
    res = out["result"]
    if res.get("blocked"):
        print(f"evaluation {out['id']} BLOCKED: {res['blocked']}", file=sys.stderr)
        return 3
    print(f"evaluation {out['id']}: {out['status']}")
    print(res["summary_markdown"])
    if a.out:
        Path(a.out).write_text(res["summary_markdown"], encoding="utf-8")
        print(f"wrote {a.out}")
    return 0 if out["status"] == "completed" else 4


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="gauntlet", description="Adversarial evaluation of migration implementations")
    sub = ap.add_subparsers(dest="cmd", required=True)

    pg = sub.add_parser("pg", help="manage the local PostgreSQL 16 cluster (no Docker)")
    pg.add_argument("action", choices=["install", "init", "start", "stop", "status", "check"])

    sub.add_parser("scenarios", help="list scenarios")
    sub.add_parser("impls", help="list implementations (references and mutants)")

    r = sub.add_parser("run", help="execute one run and persist it")
    r.add_argument("--scenario", required=True)
    r.add_argument("--impl", required=True)
    r.add_argument("--seed", type=int, default=1)
    r.add_argument("--json", action="store_true", help="print the full run record as JSON")
    r.add_argument("--no-save", action="store_true")

    b = sub.add_parser("bench", help="run the benchmark matrix")
    b.add_argument("--impls", help="comma-separated (default: all)")
    b.add_argument("--scenarios", help="comma-separated (default: all)")
    b.add_argument("--seeds", default="1,2,3")
    b.add_argument("--no-repro", action="store_true")
    b.add_argument("--no-conventional", action="store_true")
    b.add_argument("--write-results", metavar="PATH", help="write a Markdown results report (e.g. docs/RESULTS.md)")

    c = sub.add_parser("conventional", help="run the conventional functional suite")
    c.add_argument("--impl", required=True)

    sub.add_parser("list", help="list persisted runs")
    s = sub.add_parser("show", help="show a persisted run")
    s.add_argument("run_id")
    rp = sub.add_parser("report", help="Markdown report for a run, or for the latest benchmark")
    rp.add_argument("run_id", nargs="?")
    rp.add_argument("--benchmark", action="store_true")
    rr = sub.add_parser("replay", help="re-execute a persisted run's configuration and compare hashes")
    rr.add_argument("run_id")

    sv = sub.add_parser("serve", help="start the API server")
    sv.add_argument("--host", default="127.0.0.1")
    sv.add_argument("--port", type=int, default=8000)

    df = sub.add_parser("diff", help="differential comparison of implementations on one schedule")
    df.add_argument("--impls", required=True, help="comma-separated, at least two")
    df.add_argument("--scenario", required=True)
    df.add_argument("--seed", type=int, default=1)
    df.add_argument("--from-run", help="use this persisted run's exact schedule")
    ex = sub.add_parser("explore", help="bounded schedule exploration of one interleave directive")
    ex.add_argument("--scenario", required=True, help="catalog scenario or fixture (e.g. F01_backfill_race_probe)")
    ex.add_argument("--impl", required=True)
    ex.add_argument("--seed", type=int, default=1)
    ex.add_argument("--max-schedules", type=int, default=40)
    ex.add_argument("--max-preemptions", type=int, default=1)
    mn = sub.add_parser("minimize", help="minimize a persisted failing run")
    mn.add_argument("run_id")
    mn.add_argument("--invariant")
    mn.add_argument("--max-attempts", type=int, default=60)
    cd = sub.add_parser("candidate", help="external candidate implementations (validate, register, evaluate)")
    cd.add_argument("action", choices=["validate", "register", "verify", "list", "evaluate", "isolation"])
    cd.add_argument("target", nargs="?", help="validate/register: candidate directory; verify: candidate id")
    cd.add_argument("--id", dest="candidate_id", help="evaluate: a registered candidate id")
    cd.add_argument("--dir", dest="candidate_dir", help="evaluate: register this directory first, then evaluate it")
    cd.add_argument("--scenarios", help="comma-separated (default: all six benchmark scenarios)")
    cd.add_argument("--seeds", default="1")
    cd.add_argument("--refs", help="comma-separated reference implementations (default: both)")
    cd.add_argument("--isolation", default="process", choices=["process", "strict"])
    cd.add_argument("--trusted-dev-env", action="store_true",
                    help="declare this host a trusted, disposable development environment; required for process "
                         "isolation, which is NOT a security boundary (never use it for untrusted code)")
    cd.add_argument("--memory-mb", type=int)
    cd.add_argument("--cpu-seconds", type=int)
    cd.add_argument("--time-limit", type=float, help="per-run wall-clock limit in seconds")
    cd.add_argument("--explore-max-schedules", type=int, default=40)
    cd.add_argument("--explore-max-preemptions", type=int, default=1)
    cd.add_argument("--no-explore", action="store_true")
    cd.add_argument("--no-minimize", action="store_true")
    cd.add_argument("--minimize-budget", type=int, default=40)
    cd.add_argument("--out", help="evaluate: also write the summary Markdown to this file")

    xp = sub.add_parser("experiment", help="experiment reports from persisted runs")
    xp.add_argument("action", choices=["create", "report", "export"])
    xp.add_argument("target", help="create: path to a JSON config; report/export: experiment id")
    xp.add_argument("--out", help="write the report / bundle to this file")

    a = ap.parse_args(argv)

    if a.cmd == "pg":
        from gauntlet.pg import localpg
        if a.action == "check":
            from gauntlet.pg.provision import check_server
            print(json.dumps(check_server(), indent=2))
            return 0
        res = getattr(localpg, a.action)()
        return res if isinstance(res, int) else 0

    if a.cmd == "scenarios":
        from gauntlet.scenarios.catalog import SCENARIOS
        for sc in SCENARIOS.values():
            print(f"{sc.id:<34} faults={','.join(sc.faults) or '-':<14} {sc.title}")
        return 0

    if a.cmd == "impls":
        from gauntlet.impls.registry import IMPLEMENTATIONS
        for i in IMPLEMENTATIONS.values():
            extra = f" intended={i.intended_invariant}" if i.kind == "mutant" else ""
            print(f"{i.id:<26} {i.kind:<9} strategy={i.strategy}{extra}  {i.title}")
        return 0

    if a.cmd == "run":
        from gauntlet.engine.runner import execute_run
        from gauntlet.store import Store
        run = execute_run(a.scenario, a.impl, a.seed)
        if not a.no_save:
            Store().save_run(run)
        if a.json:
            print(json.dumps(run, indent=1, default=str))
        else:
            _print_run(run)
        return 0 if run["verdict"] in ("pass", "fail") else 2

    if a.cmd == "bench":
        from gauntlet.bench import run_benchmark
        from gauntlet.report import benchmark_report
        summary = run_benchmark(
            impls=a.impls.split(",") if a.impls else None,
            scenarios=a.scenarios.split(",") if a.scenarios else None,
            seeds=[int(x) for x in a.seeds.split(",")],
            reproducibility=not a.no_repro, conventional=not a.no_conventional)
        print(json.dumps({k: summary[k] for k in (
            "benchmark_id", "runs_completed", "verdicts", "invariant_violations", "mutants_detected",
            "mutants_killed_as_intended", "inconclusive_runs", "infrastructure_failures")}, indent=2))
        print("reproducibility:", summary["reproducibility"]["reproduced"], "/", summary["reproducibility"]["checked"])
        if a.write_results:
            path = Path(a.write_results)
            if not path.is_absolute():
                path = REPO_ROOT / path
            path.write_text(benchmark_report(summary), encoding="utf-8")
            print(f"wrote {path}")
        return 0

    if a.cmd == "conventional":
        from gauntlet.conventional import run_suite
        res = run_suite(a.impl)
        for t in res["tests"]:
            print(f"  {t['id']} {t['status']:<12} {t['title']}  {t['message'][:120]}")
        print(res["counts"])
        return 0

    from gauntlet.store import Store
    store = Store()
    if a.cmd == "list":
        for x in store.list_runs():
            print(f"{x['id']}  {x['created_at'][:19]}  {x['impl_id']:<24} {x['scenario_id']:<32} "
                  f"seed={x['seed']:<4} {x['verdict'] or x['status']}")
        return 0
    if a.cmd == "show":
        run = store.get_run(a.run_id)
        if run is None:
            print("no such run", file=sys.stderr)
            return 1
        _print_run(run)
        return 0
    if a.cmd == "report":
        from gauntlet.report import benchmark_report, run_report
        if a.benchmark:
            bm = store.get_benchmark()
            if not bm:
                print("no completed benchmark", file=sys.stderr)
                return 1
            print(benchmark_report(bm["summary"]))
            return 0
        run = store.get_run(a.run_id) if a.run_id else None
        if run is None:
            print("no such run", file=sys.stderr)
            return 1
        print(run_report(run))
        return 0
    if a.cmd == "replay":
        from gauntlet.execution import REPLAY_FIELDS, replay
        orig = store.get_run(a.run_id)
        if orig is None or "schedule" not in orig:
            print("no such completed run", file=sys.stderr)
            return 1
        res = replay(store, orig)  # explicit schedules are re-executed from the stored document
        again = res["run"]
        for h in REPLAY_FIELDS:
            print(f"{h:<14} {'same' if orig[h] == again[h] else 'DIFFERENT'}  {str(orig[h])[:16]}  {str(again[h])[:16]}")
        return 0
    if a.cmd == "diff":
        from gauntlet.differential import run_differential
        sched = store.get_run(a.from_run)["schedule"] if a.from_run else None
        out = run_differential(store, a.impls.split(","), a.scenario, a.seed, schedule=sched)
        res = out["result"]
        print(f"comparison {out['id']}: {res['classification'].upper()}  {res['reason']}")
        for impl, o in res["oracle"].items():
            print(f"  {impl:<26} run {o['run_id']} {o['outcome']:<14} failed={o['failed_invariants']}")
        return 0
    if a.cmd == "explore":
        from gauntlet.explore import explore
        out = explore(store, a.scenario, a.impl, a.seed,
                      limits={"max_schedules": a.max_schedules, "max_preemptions": a.max_preemptions})
        res = out["result"]
        print(f"exploration {out['id']}: {res['status']} ({res['stop_reason'] or 'frontier exhausted'}); "
              f"executed {res['schedules_executed']} {res['outcomes']}; exhaustive within bounds: "
              f"{res['exhaustive_within_bounds']}; known unexplored: {res['unexplored_known']}")
        for c in res["failure_classes"]:
            print(f"  failure class {c['id']}: {c['failed_invariants']} {c['diagnosis_categories']} x{c['count']} "
                  f"replay reproduced: {c.get('replay', {}).get('reproduced')}")
        return 0
    if a.cmd == "minimize":
        from gauntlet.minimize import minimize
        out = minimize(store, a.run_id, invariant=a.invariant, budget={"max_attempts": a.max_attempts})
        res = out["result"]
        print(f"minimization {out['id']}: {res['status']} {res.get('stop_reason') or ''}; {res.get('minimality')}")
        if res.get("reduction"):
            print("  reduction (original -> minimized):", json.dumps(res["reduction"]))
            print("  replay:", json.dumps(res["minimized"]["replay"]))
        return 0
    if a.cmd == "candidate":
        return _candidate(a, store)
    if a.cmd == "experiment":
        from gauntlet.experiment import export_bundle, generate_report, new_experiment
        if a.action == "create":
            print(new_experiment(store, json.loads(Path(a.target).read_text(encoding="utf-8"))))
            return 0
        text = (generate_report(store, a.target)["markdown"] if a.action == "report"
                else json.dumps(export_bundle(store, a.target), indent=1))
        if a.out:
            Path(a.out).write_text(text, encoding="utf-8")
            print(f"wrote {a.out}")
        else:
            print(text)
        return 0
    if a.cmd == "serve":
        import uvicorn
        from gauntlet.api.local_guard import LOOPBACK_HOSTS, allowed_hosts
        if a.host not in LOOPBACK_HOSTS:
            print(f"WARNING: --host {a.host} is not a loopback address. The GAUNTLET API has no authentication: "
                  "anyone who can reach this port can start runs and candidate evaluations. Requests are still "
                  f"refused unless their Host header names one of {sorted(allowed_hosts())} "
                  "(GAUNTLET_API_ALLOWED_HOSTS adds names). Do not expose this server on a network.",
                  file=sys.stderr)
        uvicorn.run("gauntlet.api.app:app", host=a.host, port=a.port)
        return 0
    return 1


if __name__ == "__main__":
    sys.exit(main())
