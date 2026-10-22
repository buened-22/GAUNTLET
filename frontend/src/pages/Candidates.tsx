import { useState } from "react";
import { api } from "../api";
import { AnalysisList, AnalysisStatus, downloadJson, usePolled } from "../components/analysis";
import { Markdown } from "../components/Markdown";
import { ErrorBox, Hash, Link, Loading, Panel, useAsync } from "../components/ui";

export function CandidatesPage({ id, go }: { id: string; go: (id: string) => void }) {
  return id ? <EvaluationDetail id={id} /> : <CandidateList go={go} />;
}

function CandidateList({ go }: { go: (id: string) => void }) {
  const cands = useAsync(() => api.candidates());
  const caps = useAsync(() => api.isolation());
  const scen = useAsync(() => api.scenarios());
  const [pick, setPick] = useState("");
  const [scenarios, setScenarios] = useState<string[] | null>(null);
  const [seeds, setSeeds] = useState("1");
  const [explore, setExplore] = useState(true);
  const [minimize, setMinimize] = useState(true);
  const [mode, setMode] = useState("process");
  const [trusted, setTrusted] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  if (cands.error) return <ErrorBox message={cands.error} />;
  if (!cands.data || !scen.data) return <Loading />;
  const chosen = scenarios ?? scen.data.map((s) => s.id);
  const seedList = seeds.split(/[\s,]+/).filter(Boolean).map(Number);
  const ok = !!pick && chosen.length > 0 && seedList.length > 0 && seedList.every((n) => Number.isInteger(n) && n >= 0);
  const start = async () => {
    if (busy) return;
    setBusy(true);
    setError(null);
    try {
      const r = await api.startEvaluation({ candidate_id: pick, scenarios: chosen, seeds: seedList,
        isolation: { mode, trusted_environment: trusted }, explore: explore ? { max_schedules: 40, max_preemptions: 1 } : null,
        minimize_max_attempts: minimize ? 40 : null });
      go(r.id);
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setBusy(false);
    }
  };
  return (
    <div className="stack">
      <Panel title="External candidates" sub="Implementations produced outside GAUNTLET, evaluated from immutable, content-hashed snapshots. Register a candidate directory with `python -m gauntlet candidate register DIR` (the API never accepts file paths).">
        <div className="warn-box small">No external AI coding agent has been evaluated. Candidates listed here are whatever was registered on this machine; the manifest's producer field is claimed, not verified.</div>
        {caps.data && <p className="small">Isolation on this host: <strong>process</strong> mode ({caps.data.process.process_tree_control}; {caps.data.process.resource_limits}) — <strong>not a security boundary</strong>. Strict (container) mode: {caps.data.strict.reason}.</p>}
        {cands.data.length === 0 ? <p className="muted">No candidates registered.</p> : (
          <div className="table-wrap"><table className="table">
            <thead><tr><th></th><th>Candidate</th><th>Name</th><th>Strategy</th><th>Source hash</th><th>Producer (claimed)</th><th>Registered</th></tr></thead>
            <tbody>{cands.data.map((c) => (
              <tr key={c.candidate_id}>
                <td><input type="radio" name="cand" checked={pick === c.candidate_id} onChange={() => setPick(c.candidate_id)} aria-label={`select ${c.candidate_id}`} /></td>
                <td><code>{c.candidate_id}</code></td><td>{c.name}</td><td>{c.strategy}</td><td><Hash value={c.source_hash} /></td>
                <td className="small">{c.producer ? String((c.producer as Record<string, unknown>).kind ?? "") : "not stated"}</td>
                <td className="small">{c.registered_at.slice(0, 19)}</td>
              </tr>))}</tbody></table></div>)}
      </Panel>
      <Panel title="Evaluate the selected candidate" sub="Benchmark runs as differential comparisons against the references (same schedule, own database each), bounded exploration, minimization of failures, experiment report with manifest verification.">
        <fieldset className="checks"><legend className="small">Scenarios</legend>
          {scen.data.map((s) => (
            <label key={s.id} className="row gap small"><input type="checkbox" checked={chosen.includes(s.id)}
              onChange={() => setScenarios(chosen.includes(s.id) ? chosen.filter((x) => x !== s.id) : [...chosen, s.id])} /><code>{s.id}</code></label>))}
        </fieldset>
        <div className="form-grid">
          <label><span>Seeds</span><input value={seeds} onChange={(e) => setSeeds(e.target.value)} /></label>
          <label><span>Isolation</span><select value={mode} onChange={(e) => setMode(e.target.value)}>
            <option value="process">process (development sandbox, not a security boundary)</option>
            <option value="strict">strict (container; unavailable here, fails closed)</option></select></label>
          <label className="row gap"><input type="checkbox" checked={trusted} onChange={(e) => setTrusted(e.target.checked)} />
            <span>This host is a trusted, disposable development environment (required for process mode, which is not a security boundary; never for untrusted code)</span></label>
          <label className="row gap"><input type="checkbox" checked={explore} onChange={(e) => setExplore(e.target.checked)} /><span>Exploration (≤40 schedules, ≤1 preemption)</span></label>
          <label className="row gap"><input type="checkbox" checked={minimize} onChange={(e) => setMinimize(e.target.checked)} /><span>Minimize failures</span></label>
          <div className="form-actions"><button className="btn primary" disabled={!ok || busy} onClick={start}>{busy ? "Starting…" : "Start evaluation"}</button></div>
        </div>
        {!pick && <p className="field-error">Select a candidate.</p>}
        {error && <ErrorBox message={error} />}
      </Panel>
      <Panel title="Evaluations"><AnalysisList kind="evaluation" base="/candidates" describe={(a) => String(a.config.candidate_id ?? "")} /></Panel>
    </div>
  );
}

function EvaluationDetail({ id }: { id: string }) {
  const { data: a, error } = usePolled(() => api.evaluation(id), id);
  if (error) return <ErrorBox message={error} />;
  if (!a) return <Loading />;
  const r = a.result;
  const running = a.status === "queued" || a.status === "running";
  return (
    <div className="stack">
      <Panel title={<span className="row gap">Evaluation <code>{a.id}</code> <AnalysisStatus status={a.status} /></span>}
        sub={`candidate ${a.config.candidate_id}`}
        actions={<span className="row gap">
          <Link to="/candidates" className="btn">All candidates</Link>
          {running && <button className="btn" onClick={() => api.cancelAnalysis(a.id)}>Cancel</button>}
          {r && <button className="btn" onClick={() => downloadJson(`gauntlet-evaluation-${a.id}.json`, a)}>Export JSON</button>}
          {r?.summary_markdown && <a className="btn" href={`data:text/markdown;charset=utf-8,${encodeURIComponent(r.summary_markdown)}`} download={`gauntlet-evaluation-${a.id}.md`}>Download .md</a>}
        </span>}>
        {r?.blocked && <ErrorBox message={`Blocked (nothing was executed): ${r.blocked}`} />}
        {running && r?.steps?.benchmark && <p className="small">Benchmark comparisons so far: {r.steps.benchmark.comparisons.length}</p>}
        {!r && <p className="muted">The backend reports this evaluation as <strong>{a.status}</strong>.</p>}
      </Panel>
      {r?.summary_markdown && <Panel title="Summary"><Markdown source={r.summary_markdown} /></Panel>}
      {r?.steps?.benchmark && (
        <Panel title="Linked records">
          <ul className="small">
            {r.steps.benchmark.comparisons.map((c) => <li key={c.id}>{c.scenario_id} / seed {c.seed}: <Link to={`/differential/${c.id}`}><code>{c.id}</code></Link> ({c.classification}); candidate run <Link to={`/runs/${c.candidate_run}`}><code>{c.candidate_run}</code></Link></li>)}
            {r.steps.exploration?.id && <li>exploration <Link to={`/explore/${r.steps.exploration.id}`}><code>{r.steps.exploration.id}</code></Link></li>}
            {(r.steps.minimization?.minimizations ?? []).map((m) => <li key={m.id}>minimization <Link to={`/minimize/${m.id}`}><code>{m.id}</code></Link></li>)}
            {r.steps.report?.experiment_id && <li>experiment <Link to={`/experiments/${r.steps.report.experiment_id}`}><code>{r.steps.report.experiment_id}</code></Link></li>}
          </ul>
        </Panel>)}
    </div>
  );
}
