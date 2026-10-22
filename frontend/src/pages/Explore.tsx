import { useState } from "react";
import { api, ExploredSchedule } from "../api";
import { AnalysisList, AnalysisStatus, downloadJson, usePolled } from "../components/analysis";
import { ErrorBox, Link, Loading, Panel, StatusBadge, useAsync } from "../components/ui";
import { ReplayStatus } from "./Differential";

const LIMITS: { key: string; label: string; def: number; min: number; max: number }[] = [
  { key: "max_schedules", label: "Max schedules", def: 40, min: 1, max: 500 },
  { key: "max_preemptions", label: "Max preemptions", def: 1, min: 0, max: 3 },
  { key: "max_depth", label: "Max branching depth (steps)", def: 400, min: 1, max: 5000 },
  { key: "max_events_per_run", label: "Max events per run", def: 5000, min: 50, max: 50000 },
  { key: "run_time_limit_s", label: "Time limit per run (s)", def: 120, min: 1, max: 3600 },
  { key: "max_duration_s", label: "Max total duration (s)", def: 1800, min: 1, max: 86400 },
  { key: "max_retries", label: "Retries after infra error", def: 1, min: 0, max: 5 },
];

export function ExplorePage({ id, go }: { id: string; go: (id: string) => void }) {
  return id ? <ExplorationDetail id={id} /> : <ExplorationStart go={go} />;
}

function ExplorationStart({ go }: { go: (id: string) => void }) {
  const scenarios = useAsync(() => Promise.all([api.fixtures(), api.scenarios()]).then(([a, b]) => [...a, ...b]));
  const impls = useAsync(() => api.implementations());
  const [scenario, setScenario] = useState("F01_backfill_race_probe");
  const [impl, setImpl] = useState("m1_stale_backfill");
  const [seed, setSeed] = useState("1");
  const [limits, setLimits] = useState<Record<string, string>>(Object.fromEntries(LIMITS.map((l) => [l.key, String(l.def)])));
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  if (scenarios.error || impls.error) return <ErrorBox message={(scenarios.error || impls.error)!} />;
  if (!scenarios.data || !impls.data) return <Loading />;
  const bad = LIMITS.filter((l) => !/^\d+$/.test(limits[l.key]) || Number(limits[l.key]) < l.min || Number(limits[l.key]) > l.max);
  const start = async () => {
    if (busy) return;
    setBusy(true);
    setError(null);
    try {
      const r = await api.startExploration({ scenario_id: scenario, impl_id: impl, seed: Number(seed),
        limits: Object.fromEntries(LIMITS.map((l) => [l.key, Number(limits[l.key])])) });
      go(r.id);
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setBusy(false);
    }
  };
  return (
    <div className="stack">
      <Panel title="Systematic schedule exploration" sub="Bounded, reproducible search over the statement-level interleavings of one interleave directive. Each schedule runs on a fresh database and is judged by the independent oracle. Not an exhaustive model checker: completeness is claimed only within the preemption bound, and only if nothing was skipped.">
        <div className="form-grid">
          <label><span>Scenario</span>
            <select value={scenario} onChange={(e) => setScenario(e.target.value)}>
              {scenarios.data.map((s) => <option key={s.id} value={s.id}>{s.id}{(s as { fixture?: boolean }).fixture ? " (fixture)" : ""}</option>)}
            </select></label>
          <label><span>Implementation</span>
            <select value={impl} onChange={(e) => setImpl(e.target.value)}>
              {impls.data.map((i) => <option key={i.id} value={i.id}>{i.id}</option>)}
            </select></label>
          <label><span>Seed</span><input value={seed} onChange={(e) => setSeed(e.target.value.trim())} inputMode="numeric" /></label>
          {LIMITS.map((l) => (
            <label key={l.key}><span>{l.label}</span>
              <input value={limits[l.key]} inputMode="numeric" aria-invalid={bad.includes(l)}
                onChange={(e) => setLimits({ ...limits, [l.key]: e.target.value.trim() })} /></label>))}
          <div className="form-actions"><button className="btn primary" disabled={busy || bad.length > 0 || !/^\d+$/.test(seed)} onClick={start}>{busy ? "Starting…" : "Start exploration"}</button></div>
        </div>
        {bad.length > 0 && <p className="field-error">Out of range: {bad.map((l) => `${l.label} (${l.min}–${l.max})`).join(", ")}</p>}
        {error && <ErrorBox message={error} />}
      </Panel>
      <Panel title="Explorations"><AnalysisList kind="exploration" base="/explore"
        describe={(a) => `${a.config.impl_id} × ${a.config.scenario_id} · seed ${a.config.seed} · ≤${a.config.limits?.max_preemptions} preemption(s), ≤${a.config.limits?.max_schedules} schedules`} /></Panel>
    </div>
  );
}

const choiceString = (s: ExploredSchedule, actors: string[]) =>
  s.choices.map((c) => String.fromCharCode(65 + Math.max(0, actors.indexOf(c)))).join("");

function ExplorationDetail({ id }: { id: string }) {
  const { data: a, error } = usePolled(() => api.exploration(id), id);
  const [replays, setReplays] = useState<string[]>([]);
  const [msg, setMsg] = useState<string | null>(null);
  if (error) return <ErrorBox message={error} />;
  if (!a) return <Loading />;
  const r = a.result;
  const actors = Array.from(new Set((r?.schedules ?? []).flatMap((s) => s.choices))).sort();
  const running = a.status === "queued" || a.status === "running";
  const minimize = (runId: string, inv: string) => api.startMinimization({ run_id: runId, invariant: inv, budget: { max_attempts: 60 } })
    .then((m) => { window.location.hash = `/minimize/${m.id}`; }).catch((e) => setMsg((e as Error).message));
  return (
    <div className="stack">
      <Panel title={<span className="row gap">Exploration <code>{a.id}</code> <AnalysisStatus status={a.status} /></span>}
        sub={`${a.config.impl_id} × ${a.config.scenario_id} · seed ${a.config.seed} · directive ${a.config.directive_index ?? "?"}`}
        actions={<span className="row gap">
          <Link to="/explore" className="btn">New exploration</Link>
          {running && <button className="btn" onClick={() => api.cancelAnalysis(a.id).then(() => setMsg("Cancellation requested; the exploration stops after the current execution."))}>Cancel</button>}
          {r && <button className="btn" onClick={() => downloadJson(`gauntlet-exploration-${a.id}.json`, a)}>Export JSON</button>}
        </span>}>
        {msg && <div className="note-box small">{msg}</div>}
        {!r && <p className="muted">The backend reports this exploration as <strong>{a.status}</strong>.</p>}
        {r && <>
          <p><strong>{r.schedules_executed}</strong> schedules executed ({Object.entries(r.outcomes).map(([k, v]) => `${v} ${k}`).join(", ") || "none"}) in {r.elapsed_s}s ·{" "}
            <strong>{r.unique_failure_classes}</strong> unique confirmed failure class(es).</p>
          {r.exhaustive_within_bounds
            ? <div className="note-box small">Exhaustive within bounds: {r.exhaustive_claim} (assuming deterministic execution; every failure class was replayed to check).</div>
            : <div className="warn-box small">Not exhaustive{r.stop_reason ? `: stopped because ${r.stop_reason}` : running ? ": still running" : ""}.
                {" "}{r.unexplored_known} generated schedule(s) not executed{r.unexplored_note ? ` (${r.unexplored_note})` : ""}.
                {r.counters.branch_points_beyond_depth > 0 && ` ${r.counters.branch_points_beyond_depth} branch point(s) beyond max depth were not explored.`}
                {r.counters.errored > 0 && ` ${r.counters.errored} execution(s) errored.`}
                {r.counters.nondeterministic_or_invalid > 0 && ` ${r.counters.nondeterministic_or_invalid} prefix(es) could not be replayed (nondeterminism).`}</div>}
          <p className="small muted">Limits: {JSON.stringify(a.config.limits)} · counters: {JSON.stringify(r.counters)}. {a.config.algorithm}</p>
        </>}
      </Panel>
      {r && r.failure_classes.length > 0 && (
        <Panel title="Confirmed failures" sub={r.grouping}>
          {r.failure_classes.map((c) => (
            <div key={c.id} className="note-box">
              <div className="row gap"><StatusBadge status="fail" /> <strong>{c.failed_invariants.join(", ")}</strong>
                <span className="small">diagnosis: {c.diagnosis_categories.join(", ") || "–"} · {c.count} schedule(s): {c.schedule_numbers.join(", ")}</span>
                {c.flaky && <span className="badge st-inc">≈ flaky on replay</span>}</div>
              <p className="small">Representative: schedule {c.representative.n}, run <Link to={`/runs/${c.representative.run_id}`}><code>{c.representative.run_id}</code></Link>
                {c.replay?.skipped && <> · automatic replay: <strong>skipped</strong> ({c.replay.reason})</>}
                {c.replay && !c.replay.skipped && c.replay.reproduced === null && <> · automatic replay: <strong>inconclusive</strong> ({c.replay.note})</>}
                {c.replay && !c.replay.skipped && c.replay.reproduced !== null && <> · automatic replay: <strong>{c.replay.reproduced ? "reproduced" : "NOT reproduced"}</strong> (run <Link to={`/runs/${c.replay.run_id}`}><code>{c.replay.run_id}</code></Link>; same trace {String(c.replay.same_trace)}, same outcome {String(c.replay.same_outcome)}, same choices {String(c.replay.same_choices)})</>}</p>
              <div className="row gap">
                <button className="btn small" onClick={() => api.exportSchedule(a.id, c.representative.n).then((x) => downloadJson(`gauntlet-schedule-${a.id}-${c.representative.n}.json`, x))}>Export schedule + trace</button>
                <button className="btn small" onClick={() => minimize(c.representative.run_id, c.failed_invariants.includes("S1_ACKED_DURABLE") ? "S1_ACKED_DURABLE" : c.failed_invariants[0])}>Minimize this failure</button>
              </div>
            </div>))}
        </Panel>)}
      {replays.length > 0 && <Panel title="Replays">{replays.map((x) => <ReplayStatus key={x} id={x} />)}</Panel>}
      {r && (
        <Panel title="Executed schedules" sub={`Choices per step, one letter per actor: ${actors.map((x, i) => `${String.fromCharCode(65 + i)} = ${x}`).join(", ")}. Failures first.`}>
          <div className="table-wrap"><table className="table compact">
            <thead><tr><th>#</th><th>Outcome</th><th>Preemptions</th><th>Steps</th><th>Choices</th><th>Failed invariants</th><th>Run</th><th></th></tr></thead>
            <tbody>{[...r.schedules].sort((x, y) => Number(y.outcome === "fail") - Number(x.outcome === "fail") || x.n - y.n).map((s) => (
              <tr key={s.n} className={s.outcome === "fail" ? "row-diff" : ""}>
                <td>{s.n}</td>
                <td><StatusBadge status={s.outcome === "pass" || s.outcome === "fail" || s.outcome === "inconclusive" ? s.outcome : "infra_error"} />{!["pass", "fail", "inconclusive"].includes(s.outcome) && <span className="small"> {s.outcome}</span>}</td>
                <td>{s.preemptions}</td><td>{s.steps}</td>
                <td className="mono small" title={s.choices.join(" ")}>{choiceString(s, actors)}</td>
                <td className="mono small">{s.failed_invariants.join(", ") || "–"}{s.note && <div className="muted">{s.note}</div>}</td>
                <td><Link to={`/runs/${s.run_id}`}><code>{s.run_id}</code></Link></td>
                <td className="row gap">
                  <button className="btn small" onClick={() => api.exportSchedule(a.id, s.n).then((x) => downloadJson(`gauntlet-schedule-${a.id}-${s.n}.json`, x))}>Export</button>
                  <button className="btn small" onClick={() => api.replayRun(s.run_id).then((x) => setReplays((p) => [x.id, ...p]))}>Replay</button>
                </td>
              </tr>))}</tbody>
          </table></div>
        </Panel>)}
    </div>
  );
}
