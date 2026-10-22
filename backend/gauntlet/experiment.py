"""Research-grade experiment reporting from persisted runs.

An *experiment* is a persisted, versioned configuration that selects runs (a benchmark, explicit run
ids, and/or differential / exploration analyses) and states the research question, hypotheses,
stopping rule and exclusions. A report is computed only from the persisted records it selects; every
metric states its numerator, denominator, unit of analysis and inclusion rules, and lists the run ids
behind both. Missing outcomes are never counted as passes. Nothing about AI agents is reported unless
agent runs are actually selected (none exist in this prototype).
"""

from __future__ import annotations

import copy
import hashlib
import json
import math
import re
import statistics
from datetime import datetime, timezone

from gauntlet import BENCHMARK_VERSION
from gauntlet.impls.registry import IMPLEMENTATIONS
from gauntlet.schedules import engine_fingerprint, git_revision, oracle_fingerprint
from gauntlet.store import Store

REPORT_VERSION = "experiment-report/1"
CONCLUSIVE = ("pass", "fail")
UNAVAILABLE = "unavailable (not recorded)"

DEFAULT_CONFIG = {
    "title": "Untitled experiment",
    "experiment_type": "exploratory",          # exploratory | confirmatory
    "research_question": UNAVAILABLE,
    "hypotheses": [],
    "selection": {"benchmark_id": None, "run_ids": [], "analysis_ids": []},
    "required": None,                          # {"impls": [...], "scenarios": [...], "seeds": [...]} stopping rule
    "exclusions": [],                          # [{"run_id": ..., "reason": ...}]
    "sampling": "fixed_set",                   # fixed_set | independent_random
    "preregistered": False,
    "agents": [],                              # AI agents evaluated: none in this prototype
    "prompt_version": UNAVAILABLE,
    "model_identifiers": UNAVAILABLE,
    "paired_comparisons": "mutant_vs_base",    # or list of [impl_a, impl_b]
}

_SECRET_PATTERNS = [
    (re.compile(r"(postgres(?:ql)?://)([^:/@\s]+):([^@\s]+)@"), r"\1***:***@"),
    (re.compile(r"(?i)\b(api[_-]?key|token|secret|password|passwd)\b(\s*[=:]\s*)(\"?)[^\s\",}]+"), r"\1\2\3***"),
    (re.compile(r"\bsk-[A-Za-z0-9_-]{16,}"), "sk-***"),
    (re.compile(r"(?i)([A-Z]:\\\\?Users\\\\?)[^\\\s\"]+"), r"\1~"),
    (re.compile(r"(/(?:home|Users)/)[^/\s\"]+"), r"\1~"),
]


def redact(text: str) -> str:
    for pat, rep in _SECRET_PATTERNS:
        text = pat.sub(rep, text)
    return text


# -- configuration versioning ---------------------------------------------------------------------
def new_experiment(store: Store, config: dict) -> str:
    cfg = {**copy.deepcopy(DEFAULT_CONFIG), **config}
    cfg["selection"] = {**DEFAULT_CONFIG["selection"], **(config.get("selection") or {})}
    problems = validate_config(cfg)
    if problems:
        raise ValueError("; ".join(problems))
    now = datetime.now(timezone.utc).isoformat()
    doc = {"versions": [{"version": 1, "created_at": now, "config": cfg}], "deviations": []}
    return store.create_analysis("experiment", doc, status="defined")


def revise_experiment(store: Store, experiment_id: str, changes: dict, reason: str | None) -> dict:
    """Add a new configuration version. Earlier versions are kept unchanged. A preregistered plan, or
    one whose results have already been reported, can only change with a recorded deviation reason."""
    exp = store.get_analysis(experiment_id)
    if exp is None or exp["kind"] != "experiment":
        raise KeyError(f"no experiment {experiment_id}")
    doc = exp["config"]
    latest = doc["versions"][-1]["config"]
    observed = bool((exp["result"] or {}).get("reports"))
    if (latest.get("preregistered") or observed) and not reason:
        raise ValueError("a deviation reason is required to change a preregistered or already reported plan")
    cfg = {**copy.deepcopy(latest), **changes}
    problems = validate_config(cfg)
    if problems:
        raise ValueError("; ".join(problems))
    v = len(doc["versions"]) + 1
    now = datetime.now(timezone.utc).isoformat()
    doc["versions"].append({"version": v, "created_at": now, "config": cfg})
    if reason or latest.get("preregistered") or observed:
        doc["deviations"].append({"version": v, "at": now, "reason": reason, "after_results_observed": observed,
                                  "changed_keys": sorted(k for k in changes if changes[k] != latest.get(k))})
    with store._lock, store._conn() as c:  # noqa: SLF001 - analyses config is append-only here
        c.execute("UPDATE analyses SET config_json = ?, updated_at = ? WHERE id = ?",
                  (json.dumps(doc, default=str), now, experiment_id))
    return doc


def validate_config(cfg: dict) -> list[str]:
    p = []
    if cfg.get("experiment_type") not in ("exploratory", "confirmatory"):
        p.append("experiment_type must be exploratory or confirmatory")
    if cfg.get("sampling") not in ("fixed_set", "independent_random"):
        p.append("sampling must be fixed_set or independent_random")
    sel = cfg.get("selection") or {}
    if not (sel.get("benchmark_id") or sel.get("run_ids") or sel.get("analysis_ids")):
        p.append("selection must name a benchmark_id, run_ids or analysis_ids")
    if cfg.get("experiment_type") == "confirmatory" and not cfg.get("preregistered"):
        p.append("a confirmatory experiment must be preregistered (fixed plan before results)")
    req = cfg.get("required")
    if req is not None and not all(k in req for k in ("impls", "scenarios", "seeds")):
        p.append("required must give impls, scenarios and seeds")
    return p


# -- data collection --------------------------------------------------------------------------------
def collect(store: Store, cfg: dict) -> dict:
    sel = cfg["selection"]
    runs: dict[str, dict] = {}
    excluded: dict[str, str] = {e["run_id"]: e["reason"] for e in cfg.get("exclusions", [])}
    benchmark = None
    stopping_rule = None
    repro = None
    conventional = None
    analyses = []
    if sel.get("benchmark_id"):
        benchmark = store.get_benchmark(sel["benchmark_id"])
        if benchmark is None:
            raise KeyError(f"no benchmark {sel['benchmark_id']}")
        s = benchmark["summary"] or {}
        summary_runs = {r["id"]: r for r in s.get("runs", [])}
        matrix_ids = set(summary_runs)
        rerun_ids = {d["run_b"] for d in s.get("reproducibility", {}).get("details", [])}
        for r in store.list_runs(limit=100_000, benchmark_id=sel["benchmark_id"]):
            if r["id"] in matrix_ids or r["id"] in rerun_ids:
                runs[r["id"]] = r
        # Runs the benchmark recorded but the store no longer holds are missing evidence, never dropped.
        for rid in sorted((matrix_ids | rerun_ids) - set(runs)):
            runs[rid] = _missing(rid, summary_runs.get(rid))
        for rid in rerun_ids:
            excluded.setdefault(rid, "reproducibility re-run (used only by the reproducibility metric)")
        cfgb = benchmark["config"]
        stopping_rule = {"impls": cfgb.get("impls"), "scenarios": cfgb.get("scenarios"), "seeds": cfgb.get("seeds"),
                         "source": f"benchmark {sel['benchmark_id']} configuration"}
        repro = {"checked": s.get("reproducibility", {}).get("checked"),
                 "reproduced": s.get("reproducibility", {}).get("reproduced"),
                 "pairs": [(d["run_a"], d["run_b"], d.get("reproduced") is True)
                           for d in s.get("reproducibility", {}).get("details", [])]}
        conventional = s.get("conventional")
    by_id = {r["id"]: r for r in store.list_runs(limit=100_000)} if (sel.get("run_ids") or sel.get("analysis_ids")) else {}
    for rid in sel.get("run_ids") or []:
        if rid not in by_id:
            raise KeyError(f"no run {rid}")
        runs[rid] = by_id[rid]
    explored: set[str] = set()
    for aid in sel.get("analysis_ids") or []:
        a = store.get_analysis(aid)
        if a is None:
            raise KeyError(f"no analysis {aid}")
        analyses.append(a)
        if a["kind"] == "differential":
            for r in (a["result"] or {}).get("runs", []):
                runs[r["id"]] = by_id.get(r["id"]) or _missing(r["id"], r)
        elif a["kind"] == "exploration":
            # Explored schedules are variants of one configuration, not independent runs: they are reported
            # only by the exploration metric (M9), never in run-level rates.
            explored |= {s_["run_id"] for s_ in (a["result"] or {}).get("schedules", [])}
    if cfg.get("required"):
        stopping_rule = {**cfg["required"], "source": "experiment configuration"}
    versions = sorted({str(r.get("benchmark_version")) for i, r in runs.items()
                       if i not in excluded and not r.get("_missing")})
    if len(versions) > 1 and not cfg.get("allow_mixed_versions"):
        raise ValueError(f"the selection combines runs from different benchmark versions {versions}; results from "
                         "different versions are not comparable run for run. Narrow the selection, or set "
                         "allow_mixed_versions: true to report them together with a warning.")
    return {"runs": runs, "excluded": excluded, "benchmark": benchmark, "stopping_rule": stopping_rule,
            "reproducibility": repro, "conventional": conventional, "analyses": analyses,
            "benchmark_versions": versions, "explored_run_ids": sorted(explored)}


def _missing(rid: str, known: dict | None) -> dict:
    """Placeholder for a run that the selection references but the store does not contain."""
    k = known or {}
    return {"id": rid, "verdict": None, "_missing": True, "impl_id": k.get("impl_id"), "impl_kind": k.get("impl_kind"),
            "scenario_id": k.get("scenario_id"), "seed": k.get("seed"), "failed_invariants": [],
            "benchmark_version": None, "duration_ms": None}


# -- metrics ---------------------------------------------------------------------------------------
def wilson(k: int, n: int, z: float = 1.959964) -> list[float] | None:
    if n == 0:
        return None
    p = k / n
    d = 1 + z * z / n
    c = (p + z * z / (2 * n)) / d
    h = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / d
    return [round(max(0.0, c - h), 4), round(min(1.0, c + h), 4)]


def _metric(mid, name, num_ids, den_ids, *, unit, inclusion, treatment, sampling, note=None, per=None):
    k, n = len(num_ids), len(den_ids)
    m = {"id": mid, "name": name, "numerator": k, "denominator": n,
         "value": round(k / n, 4) if n else None, "unit_of_analysis": unit, "inclusion": inclusion,
         "treatment_of_non_conclusive": treatment,
         "numerator_ids": sorted(num_ids), "denominator_ids": sorted(den_ids)}
    if n == 0:
        m["value_note"] = "undefined: empty denominator"
    if sampling == "independent_random" and n:
        m["interval_95"] = wilson(k, n)
        m["interval_method"] = "Wilson score interval, assuming independent random sampling of units (declared in config)"
    else:
        m["interval_95"] = None
        m["interval_method"] = ("not estimated: the units are a fixed, purposively constructed set (hand-written "
                                "implementations, fixed scenarios and seeds), and re-executing the same deterministic "
                                "configuration is not independent evidence; a sampling interval would misstate the "
                                "uncertainty") if sampling != "independent_random" else "not estimated: empty denominator"
    if n and n < 10:
        m["small_sample"] = f"n = {n}: evidence is limited; do not generalize"
    if note:
        m["note"] = note
    if per:
        m["breakdown"] = per
    return m


def compute(data: dict, cfg: dict) -> dict:
    runs, excluded = data["runs"], data["excluded"]
    sampling = cfg.get("sampling", "fixed_set")
    included = {i: r for i, r in runs.items() if i not in excluded}
    verdict_of = lambda r: r.get("verdict") or "missing"  # noqa: E731
    counts = {v: sorted(i for i, r in included.items() if verdict_of(r) == v)
              for v in ("pass", "fail", "inconclusive", "infra_error", "missing")}
    conclusive = {i for i, r in included.items() if verdict_of(r) in CONCLUSIVE}
    metrics = []
    T_CONCL = "inconclusive, infrastructure-error, timed-out and missing runs are excluded from the denominator and reported in M1"
    metrics.append({"id": "M1", "name": "Run outcomes (all included runs)", "unit_of_analysis": "run",
                    "counts": {k: len(v) for k, v in counts.items()}, "ids": counts,
                    "excluded": [{"run_id": k, "reason": v} for k, v in sorted(excluded.items()) if k in runs],
                    "inclusion": "selected runs minus exclusions"})
    metrics.append(_metric("M2", "Adversarial invariant-violation rate", counts["fail"], conclusive, unit="run",
                           inclusion="included runs with a conclusive verdict (pass or fail)", treatment=T_CONCL,
                           sampling=sampling,
                           per={kind: {"numerator": sum(1 for i in counts["fail"] if included[i].get("impl_kind") == kind),
                                       "denominator": sum(1 for i in conclusive if included[i].get("impl_kind") == kind)}
                                for kind in ("reference", "mutant", "candidate")
                                if kind != "candidate" or any(r.get("impl_kind") == "candidate" for r in included.values())}))
    metrics.append(_metric("M3", "Share of all included runs with a confirmed violation", counts["fail"], included,
                           unit="run", inclusion="all included runs", sampling=sampling,
                           treatment="non-conclusive runs stay in the denominator and are never counted as passes"))
    # Known-faulty detection and intended kills (unit: implementation)
    mutants = sorted({r["impl_id"] for r in included.values() if r.get("impl_kind") == "mutant"})
    det_num, det_den, kill_num, kill_den, undecided = [], [], [], [], []
    for m in mutants:
        mr = [r for r in included.values() if r["impl_id"] == m]
        conc = [r for r in mr if verdict_of(r) in CONCLUSIVE]
        if not conc:
            undecided.append(m)
            continue
        det_den.append(m)
        if any(verdict_of(r) == "fail" for r in conc):
            det_num.append(m)
        info = IMPLEMENTATIONS.get(m)
        if info:
            ir = [r for r in conc if r["scenario_id"] in info.intended_scenarios]
            if ir:
                kill_den.append(m)
                if any(info.intended_invariant in (r.get("failed_invariants") or []) for r in ir):
                    kill_num.append(m)
    note = f"mutants with no conclusive run (excluded): {undecided}" if undecided else None
    metrics.append(_metric("M4", "Detection rate on known-faulty implementations", det_num, det_den, unit="mutant implementation",
                           inclusion="mutants with at least one conclusive included run; detected = at least one FAIL run",
                           treatment=T_CONCL, sampling=sampling, note=note))
    metrics.append(_metric("M5", "Killed by the intended invariant", kill_num, kill_den, unit="mutant implementation",
                           inclusion="mutants with a conclusive included run in one of their intended scenarios; killed = "
                                     "the intended invariant FAILs in such a run", treatment=T_CONCL, sampling=sampling))
    refs = [i for i in conclusive if included[i].get("impl_kind") == "reference"]
    metrics.append(_metric("M6", "False-positive rate on reference implementations",
                           [i for i in refs if verdict_of(included[i]) == "fail"], refs, unit="run",
                           inclusion="conclusive included runs of reference implementations", treatment=T_CONCL,
                           sampling=sampling,
                           note="references are validated only by this same oracle and suite; a 0 here is consistency, "
                                "not proof of correctness"))
    # Conventional suite (unit: test execution)
    conv = data.get("conventional")
    if conv:
        passed, decided, infra = [], [], []
        for impl, rec in sorted(conv.items()):
            for t in rec.get("tests", []):
                tid = f"{impl}:{t['id']}"
                if t["status"] == "infra_error":
                    infra.append(tid)
                    continue
                decided.append(tid)
                if t["status"] == "pass":
                    passed.append(tid)
        metrics.append(_metric("M7", "Conventional test pass rate", passed, decided, unit="conventional test execution",
                               inclusion="the benchmark's recorded conventional suite results",
                               treatment=f"tests ending in infrastructure error are excluded from the denominator: {infra}",
                               sampling=sampling))
    else:
        metrics.append({"id": "M7", "name": "Conventional test pass rate", "value": None,
                        "value_note": UNAVAILABLE + ": no conventional results in the selection"})
    rp = data.get("reproducibility")
    if rp and rp["checked"]:
        metrics.append(_metric("M8", "Reproducibility of re-executed runs",
                               [b for a, b, same in rp["pairs"] if same], [b for a, b, _ in rp["pairs"]],
                               unit="re-executed run (same scenario, implementation, seed)",
                               inclusion="benchmark reproducibility re-runs; identical = same schedule, trace, outcome and verdict hashes",
                               treatment="a re-run that errored counts as not reproduced", sampling=sampling))
    else:
        metrics.append({"id": "M8", "name": "Reproducibility of re-executed runs", "value": None,
                        "value_note": UNAVAILABLE + ": no reproducibility re-runs in the selection"})
    expl = [a for a in data["analyses"] if a["kind"] == "exploration"]
    if expl:
        metrics.append({"id": "M9", "name": "Schedule exploration", "unit_of_analysis": "exploration",
                        "explorations": [{"id": a["id"], "impl_id": a["config"]["impl_id"], "scenario_id": a["config"]["scenario_id"],
                                          "status": a["status"], "schedules_executed": a["result"]["schedules_executed"],
                                          "unique_failure_classes": a["result"]["unique_failure_classes"],
                                          "exhaustive_within_bounds": a["result"]["exhaustive_within_bounds"],
                                          "max_preemptions": a["config"]["limits"]["max_preemptions"]} for a in expl],
                        "note": "schedule counts are executions of distinct schedules, not independent runs; failure "
                                "classes are symptom groups, not root causes"})
    durs = [r["duration_ms"] for r in included.values() if r.get("duration_ms") is not None]
    metrics.append({"id": "M10", "name": "Runtime (observational)", "unit_of_analysis": "run",
                    "n": len(durs), "median_s": round(statistics.median(durs) / 1000, 2) if durs else None,
                    "total_s": round(sum(durs) / 1000, 1) if durs else None,
                    "note": "wall-clock time on one machine; not a performance result"})
    metrics.append({"id": "M11", "name": "Cost", "value": None, "value_note": UNAVAILABLE + ": no cost data is recorded"})
    return {"metrics": metrics, "paired": paired(included, cfg), "completeness": completeness(included, data)}


def paired(included: dict, cfg: dict) -> list[dict]:
    """Matched (scenario, seed) cells for pairs of implementations; descriptive counts only."""
    spec = cfg.get("paired_comparisons", "mutant_vs_base")
    if spec == "mutant_vs_base":
        pairs = sorted({(IMPLEMENTATIONS[r["impl_id"]].base, r["impl_id"]) for r in included.values()
                        if r.get("impl_kind") == "mutant" and r["impl_id"] in IMPLEMENTATIONS
                        and IMPLEMENTATIONS[r["impl_id"]].base})
    else:
        pairs = [tuple(p) for p in spec or []]
    cells: dict[tuple, dict] = {}
    for r in included.values():
        cells.setdefault((r["scenario_id"], r["seed"]), {}).setdefault(r["impl_id"], r)
    out = []
    for a, b in pairs:
        t = {"both_pass": 0, "only_a_fails": 0, "only_b_fails": 0, "both_fail": 0, "unmatched_or_non_conclusive": 0}
        for c in cells.values():
            ra, rb = c.get(a), c.get(b)
            if not ra and not rb:
                continue
            if not ra or not rb or ra.get("verdict") not in CONCLUSIVE or rb.get("verdict") not in CONCLUSIVE:
                t["unmatched_or_non_conclusive"] += 1
                continue
            fa, fb = ra["verdict"] == "fail", rb["verdict"] == "fail"
            t["both_fail" if fa and fb else "only_a_fails" if fa else "only_b_fails" if fb else "both_pass"] += 1
        out.append({"a": a, "b": b, **t,
                    "note": "matched on (scenario, seed): same schedule definition; descriptive counts, no significance test"})
    return out


def completeness(included: dict, data: dict) -> dict:
    rule = data.get("stopping_rule")
    if not rule:
        return {"status": "not_assessed", "reason": "no stopping rule configured (no required impls x scenarios x seeds)"}
    missing, unresolved, done = [], [], 0
    for i in rule["impls"]:
        for s in rule["scenarios"]:
            for seed in rule["seeds"]:
                rs = [r for r in included.values() if r["impl_id"] == i and r["scenario_id"] == s and r["seed"] == seed]
                if not rs:
                    missing.append([i, s, seed])
                elif not any(r.get("verdict") in CONCLUSIVE for r in rs):
                    unresolved.append([i, s, seed, sorted({r.get("verdict") for r in rs})])
                else:
                    done += 1
    total = len(rule["impls"]) * len(rule["scenarios"]) * len(rule["seeds"])
    return {"status": "complete" if not missing and not unresolved else "incomplete", "rule": rule,
            "cells_required": total, "cells_conclusive": done, "missing_cells": missing, "unresolved_cells": unresolved}


# -- report and package ----------------------------------------------------------------------------
def manifest(store: Store, data: dict, metrics: list[dict]) -> dict:
    used = set()
    for m in metrics:
        used |= set(m.get("numerator_ids", [])) | set(m.get("denominator_ids", []))
        for ids in (m.get("ids") or {}).values():
            used |= set(ids)
    arts = []
    for rid in sorted(data["runs"]):
        r = data["runs"][rid]
        arts.append({"type": "run", "id": rid, "record_sha256": store.run_record_sha256(rid),
                     "verdict": r.get("verdict"), "schedule_hash": r.get("schedule_hash"), "trace_hash": r.get("trace_hash"),
                     "outcome_hash": r.get("outcome_hash"), "excluded": data["excluded"].get(rid),
                     "supports_metrics": sorted(m["id"] for m in metrics if rid in set(m.get("numerator_ids", []))
                                                | set(m.get("denominator_ids", []))
                                                | {x for ids in (m.get("ids") or {}).values() for x in ids})})
    if data["benchmark"]:
        b = data["benchmark"]
        arts.append({"type": "benchmark", "id": b["id"],
                     "summary_sha256": hashlib.sha256(json.dumps(b["summary"], sort_keys=True, default=str).encode()).hexdigest()})
    for a in data["analyses"]:
        arts.append({"type": a["kind"], "id": a["id"], "status": a["status"],
                     "result_sha256": hashlib.sha256(json.dumps(a["result"], sort_keys=True, default=str).encode()).hexdigest()})
    unreferenced = sorted(set(data["runs"]) - used)
    return {"artifacts": arts, "runs_selected": len(data["runs"]), "runs_referenced_by_metrics": len(used & set(data["runs"])),
            "runs_not_referenced": unreferenced,
            "code": {"benchmark_version": BENCHMARK_VERSION, "git": git_revision(), "oracle_fingerprint": oracle_fingerprint(),
                     "engine_fingerprint": engine_fingerprint(), "report_version": REPORT_VERSION},
            "store": {"file": data.get("store_file")}}


def _fmt(m: dict) -> str:
    if m.get("value") is None and "numerator" not in m:
        return m.get("value_note", "n/a")
    v = "undefined" if m["value"] is None else f"{m['value']:.4f}"
    ci = f"; 95% interval {m['interval_95']}" if m.get("interval_95") else ""
    return f"{m['numerator']} / {m['denominator']} = {v}{ci}"


def verify_manifest(store: Store, manifest: dict) -> dict:
    """Re-hash every artifact a report's manifest names and report anything missing or changed since."""
    missing, modified, checked = [], [], 0
    for a in manifest.get("artifacts", []):
        checked += 1
        if a["type"] == "run":
            now = store.run_record_sha256(a["id"])
            if now is None:
                missing.append(a["id"])
            elif a.get("record_sha256") and now != a["record_sha256"]:
                modified.append(a["id"])
        elif a["type"] == "benchmark":
            b = store.get_benchmark(a["id"])
            if b is None:
                missing.append(a["id"])
            elif hashlib.sha256(json.dumps(b["summary"], sort_keys=True, default=str).encode()).hexdigest() != a["summary_sha256"]:
                modified.append(a["id"])
        else:
            x = store.get_analysis(a["id"])
            if x is None:
                missing.append(a["id"])
            elif hashlib.sha256(json.dumps(x["result"], sort_keys=True, default=str).encode()).hexdigest() != a["result_sha256"]:
                modified.append(a["id"])
    return {"ok": not missing and not modified, "checked": checked, "missing": sorted(missing), "modified": sorted(modified)}


def _provenance_statement(cfg: dict, runs: dict) -> str:
    """Who produced the evaluated implementations, derived from the run records, never from claims."""
    kinds = {r.get("impl_kind") for r in runs.values() if r.get("impl_kind")}
    external = sorted({r["impl_id"] for r in runs.values() if r.get("impl_kind") == "candidate"})
    parts = []
    if kinds & {"reference", "mutant"}:
        parts.append("references and mutants are hand-written")
    if external:
        claimed = cfg.get("candidate_producer_claimed")
        parts.append(f"{len(external)} external candidate(s) {external} were evaluated from content-hashed snapshots; "
                     f"their producer is not verified by GAUNTLET"
                     + (f" (manifest claims: {claimed})" if claimed else ""))
    line = "* Implementations: " + "; ".join(parts) + "."
    if cfg.get("agents"):
        return line + chr(10) + ("* Agents recorded in the experiment plan (operator-supplied, not verified by "
                                 f"GAUNTLET): {cfg['agents']}.")
    return line + chr(10) + "* No external AI coding agent was evaluated."


def render_markdown(exp_id: str, version: dict, deviations: list, result: dict) -> str:
    cfg = version["config"]
    mets = {m["id"]: m for m in result["metrics"]}
    comp = result["completeness"]
    L = [f"# {cfg['title']}", "", f"Experiment `{exp_id}`, configuration version {version['version']} "
         f"({cfg['experiment_type']}{', preregistered' if cfg.get('preregistered') else ''}). "
         f"Report format `{REPORT_VERSION}`, generated from persisted GAUNTLET records only.", ""]
    L += ["## 1. Executive summary", ""]
    if len(result.get("benchmark_versions") or []) > 1:
        L += [f"> **Warning: mixed benchmark versions {result['benchmark_versions']}.** Runs from different versions "
              "use different schedules and are not comparable run for run; this combination was explicitly allowed "
              "by the configuration.", ""]
    m1 = mets["M1"]["counts"]
    L.append(f"* Runs included: {sum(m1.values())} (pass {m1['pass']}, fail {m1['fail']}, inconclusive {m1['inconclusive']}, "
             f"infrastructure error {m1['infra_error']}, missing {m1['missing']}); excluded: {len(mets['M1']['excluded'])}.")
    for k in ("M2", "M4", "M5", "M6", "M7", "M8"):
        if k in mets:
            L.append(f"* {mets[k]['name']}: {_fmt(mets[k])}.")
    L.append(f"* Completeness: **{comp['status']}**" + (f" ({comp['cells_conclusive']}/{comp['cells_required']} required cells "
             f"conclusive)" if comp.get("cells_required") else f" ({comp.get('reason')})") + ".")
    L.append(_provenance_statement(cfg, result["_runs"]))
    L += ["", "## 2. Research question and hypotheses", "", f"Research question: {cfg['research_question']}", ""]
    L += [f"* {h}" for h in cfg.get("hypotheses") or ["(none stated)"]]
    L += ["", "## 3. Benchmark scope and system under test", "",
          "Task family: online migration of `orders.total NUMERIC(12,2)` to `orders.total_cents BIGINT` while legacy and new "
          "application versions share one PostgreSQL 16 database. Implementations are executed by the GAUNTLET engine "
          "(statement-gated actor processes, fresh database per run).", "",
          f"Implementations in the selection: {sorted({r['impl_id'] for r in result['_runs'].values() if r.get('impl_id')})}.",
          f"Scenarios: {sorted({r['scenario_id'] for r in result['_runs'].values() if r.get('scenario_id')})}.", "",
          f"Agents recorded in the plan: {cfg.get('agents') or 'none'}; prompt version: {cfg['prompt_version']}; "
          f"model identifiers: {cfg['model_identifiers']}.", ""]
    L += ["## 4. Methodology and comparison protocol", "",
          "Each run executes a schedule (a pure function of scenario, seed and benchmark version, or an explicit derived "
          "schedule) on a newly created database; an independent oracle judges the run from the client ledger and raw "
          "snapshots. Paired comparisons match runs on (scenario, seed), i.e. on the same schedule definition.", ""]
    L += ["## 5. Invariants and scoring definitions", ""]
    for m in result["metrics"]:
        if "numerator" in m:
            L.append(f"* **{m['id']} {m['name']}**: numerator / denominator over unit *{m['unit_of_analysis']}*; "
                     f"inclusion: {m['inclusion']}; non-conclusive: {m['treatment_of_non_conclusive']}.")
    L += ["", "Run verdicts: pass only if no invariant FAILs, none is inconclusive and no infrastructure error occurred "
          "(see docs/INVARIANTS.md).", ""]
    L += ["## 6. Experimental configuration", "", "```json",
          json.dumps({k: v for k, v in cfg.items() if k != "hypotheses"}, indent=1, sort_keys=True, default=str), "```", ""]
    if deviations:
        L += ["Deviations from earlier configuration versions:", ""] + [
            f"* version {d['version']}: {d['reason']} (changed {d['changed_keys']}; after results observed: "
            f"{d['after_results_observed']})" for d in deviations] + [""]
    L += ["## 7. Results", "", "| Metric | Result | Unit | Interval |", "|---|---|---|---|"]
    for m in result["metrics"]:
        if "numerator" in m:
            L.append(f"| {m['id']} {m['name']} | {_fmt(m).split(';')[0]} | {m['unit_of_analysis']} | "
                     f"{m['interval_95'] if m.get('interval_95') else 'not estimated'} |")
    L += ["", "Uncertainty: " + next((m["interval_method"] for m in result["metrics"] if "interval_method" in m),
                                     "not applicable"), ""]
    if mets.get("M2", {}).get("breakdown"):
        L += [f"M2 by implementation kind: {mets['M2']['breakdown']}.", ""]
    small = [m["id"] for m in result["metrics"] if m.get("small_sample")]
    if small:
        L += [f"Small denominators (n < 10) in {small}: evidence is limited.", ""]
    if result["paired"]:
        L += ["Paired comparisons (matched scenario and seed; descriptive):", "",
              "| A | B | both pass | only A fails | only B fails | both fail | unmatched / non-conclusive |", "|---|---|---|---|---|---|---|"]
        L += [f"| `{p['a']}` | `{p['b']}` | {p['both_pass']} | {p['only_a_fails']} | {p['only_b_fails']} | {p['both_fail']} | "
              f"{p['unmatched_or_non_conclusive']} |" for p in result["paired"]]
        L.append("")
    for k in ("M9", "M10", "M11"):
        if k in mets:
            L.append(f"* {mets[k]['name']}: " + json.dumps({x: y for x, y in mets[k].items() if x not in ("id", "name")},
                                                          default=str))
    L += ["", "## 8. Failure analysis", ""]
    for f in result["failure_examples"]:
        L.append(f"* Run `{f['run_id']}` ({f['impl_id']} × {f['scenario_id']}, seed {f['seed']}): FAIL {f['failed']}; "
                 f"trigger event seq {f['trigger_seq']}; replay with `python -m gauntlet replay {f['run_id']}`.")
    if not result["failure_examples"]:
        L.append("No failing runs in the selection.")
    L += ["", "## 9. Limitations and threats to validity", "",
          "* All implementations are hand-written references and single-point mutants by the same author as the "
          "scenarios and oracle; they are not a sample of real or AI-generated code.",
          "* Results apply to one task family, one database (PostgreSQL 16) and one machine.",
          "* Interleavings are explored at SQL-statement granularity only; exploration covers bounded preemptions of one "
          "interleave directive and applies no partial-order reduction.",
          "* Oracle diagnoses state what the evidence is consistent with, not proven root causes.",
          "* All validation is author-run; nothing here has been independently reproduced.", ""]
    if comp["status"] != "complete":
        L += [f"* This experiment is **{comp['status']}**: " + (
            f"missing cells {comp['missing_cells'][:10]}, unresolved cells {comp['unresolved_cells'][:10]}."
            if comp.get("cells_required") else comp.get("reason", "")), ""]
    L += ["## 10. Reproduction instructions", "",
          "1. Check out the source revision in the manifest and install as in README.md.",
          "2. Re-execute any listed run with `python -m gauntlet replay <run_id>` against a copy of the results database; "
          "compare schedule, trace and outcome hashes (wall-clock timing is not reproduced).",
          f"3. Regenerate this report: `python -m gauntlet experiment report {exp_id}`.", ""]
    L += ["## 11. Evidence-supported conclusions", ""]
    L += [f"* Observed: {mets['M4']['numerator']} of {mets['M4']['denominator']} known-faulty implementations had at least "
          f"one failing run, and {mets['M6']['numerator']} of {mets['M6']['denominator']} reference runs failed, in this "
          "selection. These describe this fixed set of implementations and schedules only; they are not estimates of "
          "how often real or AI-written migrations are unsafe."]
    L += ["", "## 12. Machine-readable results and artifact manifest", "",
          "The export bundle (`/api/experiments/<id>/export`) contains the configuration versions, all metrics with "
          "their numerator and denominator run ids, the paired tables, the completeness check and the manifest of "
          "every supporting artifact with its SHA-256.", ""]
    return "\n".join(L) + "\n"


def generate_report(store: Store, experiment_id: str, *, record: bool = True) -> dict:
    exp = store.get_analysis(experiment_id)
    if exp is None or exp["kind"] != "experiment":
        raise KeyError(f"no experiment {experiment_id}")
    version = exp["config"]["versions"][-1]
    cfg = version["config"]
    data = collect(store, cfg)
    data["store_file"] = store.path.name
    computed = compute(data, cfg)
    included = {i: r for i, r in data["runs"].items() if i not in data["excluded"]}
    fails = []
    for rid in sorted(i for i, r in included.items() if r.get("verdict") == "fail")[:5]:
        full = store.get_run(rid)
        trig = min((i.get("trigger_seq") for i in full["invariants"] if i["status"] == "fail" and i.get("trigger_seq")),
                   default=None)
        fails.append({"run_id": rid, "impl_id": full["impl_id"], "scenario_id": full["scenario_id"], "seed": full["seed"],
                      "failed": [i["id"] for i in full["invariants"] if i["status"] == "fail"], "trigger_seq": trig,
                      "trace_excerpt": [{k: v for k, v in e.items() if k not in ("t_ms", "backend_pid", "blockers")}
                                        for e in full["events"] if trig and trig - 6 <= e["seq"] <= trig]})
    result = {**computed, "failure_examples": fails, "_runs": included,
              "benchmark_versions": data["benchmark_versions"], "explored_runs_excluded": len(data["explored_run_ids"])}
    md = render_markdown(experiment_id, version, exp["config"]["deviations"], result)
    result.pop("_runs")
    man = manifest(store, data, computed["metrics"])
    report = {"experiment_id": experiment_id, "config_version": version["version"], "report_version": REPORT_VERSION,
              "markdown": redact(md), "markdown_sha256": hashlib.sha256(redact(md).encode()).hexdigest(),
              "results": json.loads(redact(json.dumps(result, default=str))), "manifest": json.loads(redact(json.dumps(man, default=str)))}
    prev = (exp["result"] or {}).get("latest_manifest")
    if prev:  # evidence used by the previous report: is it still present and unchanged?
        report["evidence_check_against_previous_report"] = verify_manifest(store, prev)
    if record:
        res = exp["result"] or {}
        res.setdefault("reports", []).append({"at": datetime.now(timezone.utc).isoformat(), "config_version": version["version"],
                                             "markdown_sha256": report["markdown_sha256"],
                                             "completeness": computed["completeness"]["status"]})
        res["latest_manifest"] = report["manifest"]
        store.update_analysis(experiment_id, status=f"reported ({computed['completeness']['status']})", result=res)
    return report


def export_bundle(store: Store, experiment_id: str) -> dict:
    rep = generate_report(store, experiment_id, record=False)
    exp = store.get_analysis(experiment_id)
    bundle = {"format": "gauntlet-experiment-bundle/1", "exported_at": datetime.now(timezone.utc).isoformat(),
              "experiment": {"id": experiment_id, "versions": exp["config"]["versions"], "deviations": exp["config"]["deviations"]},
              "report": rep,
              "metric_definitions": [{k: m.get(k) for k in ("id", "name", "unit_of_analysis", "inclusion",
                                                             "treatment_of_non_conclusive", "interval_method")}
                                     for m in rep["results"]["metrics"]],
              "environment_note": "PostgreSQL version and Python version per run are in each run record; the original "
                                  "machine state (OS load, file system) cannot be reconstructed."}
    text = redact(json.dumps(bundle, default=str, sort_keys=True))
    out = json.loads(text)
    out["bundle_sha256"] = hashlib.sha256(text.encode()).hexdigest()
    return out
