import { useState } from "react";
import { api, EvidenceCheck, ExperimentReport, Metric } from "../api";
import { AnalysisList, AnalysisStatus, downloadJson, usePolled } from "../components/analysis";
import { Markdown } from "../components/Markdown";
import { ErrorBox, Json, Link, Loading, Panel, useAsync } from "../components/ui";

export function ExperimentsPage({ id, go }: { id: string; go: (id: string) => void }) {
  return id ? <ExperimentDetail id={id} /> : <ExperimentCreate go={go} />;
}

const csv = (s: string) => s.split(/[\s,]+/).map((x) => x.trim()).filter(Boolean);

function ExperimentCreate({ go }: { go: (id: string) => void }) {
  const benches = useAsync(() => api.benchmarks());
  const analyses = useAsync(() => Promise.all([api.analyses("differential"), api.analyses("exploration")]).then(([a, b]) => [...a, ...b]));
  const [f, setF] = useState({ title: "", experiment_type: "exploratory", preregistered: false, research_question: "", hypotheses: "",
    benchmark_id: "", run_ids: "", analysis_ids: [] as string[], sampling: "fixed_set", req_impls: "", req_scenarios: "", req_seeds: "" });
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const set = (k: string, v: unknown) => setF({ ...f, [k]: v });
  const create = async () => {
    if (busy) return;
    setBusy(true);
    setError(null);
    const required = f.req_impls || f.req_scenarios || f.req_seeds
      ? { impls: csv(f.req_impls), scenarios: csv(f.req_scenarios), seeds: csv(f.req_seeds).map(Number) } : null;
    try {
      const r = await api.createExperiment({
        title: f.title || "Untitled experiment", experiment_type: f.experiment_type, preregistered: f.preregistered,
        research_question: f.research_question || "unavailable (not recorded)", hypotheses: f.hypotheses.split("\n").filter(Boolean),
        selection: { benchmark_id: f.benchmark_id || null, run_ids: csv(f.run_ids), analysis_ids: f.analysis_ids },
        sampling: f.sampling, required });
      go(r.id);
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setBusy(false);
    }
  };
  return (
    <div className="stack">
      <Panel title="Experiment reports" sub="Define an experiment over persisted runs and generate a report from those records only. Every metric states its numerator, denominator and unit; missing or non-conclusive results are never counted as passes.">
        <div className="form-grid">
          <label><span>Title</span><input value={f.title} onChange={(e) => set("title", e.target.value)} /></label>
          <label><span>Type</span><select value={f.experiment_type} onChange={(e) => set("experiment_type", e.target.value)}>
            <option value="exploratory">exploratory</option><option value="confirmatory">confirmatory (requires preregistration)</option></select></label>
          <label className="row gap"><input type="checkbox" checked={f.preregistered} onChange={(e) => set("preregistered", e.target.checked)} /><span>Preregistered (plan fixed before results)</span></label>
          <label><span>Sampling of units</span><select value={f.sampling} onChange={(e) => set("sampling", e.target.value)}>
            <option value="fixed_set">fixed, purposively constructed set (no intervals)</option>
            <option value="independent_random">independent random sample (Wilson intervals)</option></select></label>
        </div>
        <div className="form-grid">
          <label><span>Research question</span><input value={f.research_question} onChange={(e) => set("research_question", e.target.value)} /></label>
          <label><span>Hypotheses (one per line)</span><textarea rows={2} value={f.hypotheses} onChange={(e) => set("hypotheses", e.target.value)} /></label>
        </div>
        <h3>Selection</h3>
        <div className="form-grid">
          <label><span>Benchmark</span><select value={f.benchmark_id} onChange={(e) => set("benchmark_id", e.target.value)}>
            <option value="">none</option>{(benches.data ?? []).map((b) => <option key={b.id} value={b.id}>{b.id} ({b.status})</option>)}</select></label>
          <label><span>Run ids (comma or space separated)</span><input value={f.run_ids} onChange={(e) => set("run_ids", e.target.value)} /></label>
          <label><span>Analyses</span><select multiple value={f.analysis_ids} onChange={(e) => set("analysis_ids", Array.from(e.target.selectedOptions).map((o) => o.value))}>
            {(analyses.data ?? []).map((a) => <option key={a.id} value={a.id}>{a.id} · {a.kind} · {a.status}</option>)}</select></label>
        </div>
        <h3>Stopping rule (optional; required for a completeness claim)</h3>
        <div className="form-grid">
          <label><span>Required implementations</span><input value={f.req_impls} onChange={(e) => set("req_impls", e.target.value)} /></label>
          <label><span>Required scenarios</span><input value={f.req_scenarios} onChange={(e) => set("req_scenarios", e.target.value)} /></label>
          <label><span>Required seeds</span><input value={f.req_seeds} onChange={(e) => set("req_seeds", e.target.value)} /></label>
          <div className="form-actions"><button className="btn primary" disabled={busy} onClick={create}>Create experiment</button></div>
        </div>
        <p className="small muted">For a benchmark selection the benchmark's own matrix is the stopping rule unless one is given here.</p>
        {error && <ErrorBox message={error} />}
      </Panel>
      <Panel title="Experiments"><AnalysisList kind="experiment" base="/experiments" describe={(a) => String(a.config.title ?? "")} /></Panel>
    </div>
  );
}

function MetricRow({ m }: { m: Metric }) {
  const has = m.numerator !== undefined;
  return (
    <tr>
      <td><strong>{m.id}</strong> {m.name}</td>
      <td>{has ? <>{m.numerator} / {m.denominator} = {m.value ?? "undefined"}</> : m.value_note ?? (m.counts ? JSON.stringify(m.counts) : "see details")}
        {m.small_sample && <div className="small txt-crit">{m.small_sample}</div>}</td>
      <td className="small">{m.unit_of_analysis ?? "–"}</td>
      <td className="small">{m.inclusion ?? "–"}{m.treatment_of_non_conclusive && <div className="muted">{m.treatment_of_non_conclusive}</div>}</td>
      <td className="small">{m.interval_95 ? JSON.stringify(m.interval_95) : (m.interval_method ? "not estimated" : "–")}</td>
    </tr>
  );
}

function ExperimentDetail({ id }: { id: string }) {
  const { data: a, error, reload } = usePolled(() => api.experiment(id), id);
  const [rep, setRep] = useState<ExperimentReport | null>(null);
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string | null>(null);
  const [changes, setChanges] = useState("{}");
  const [reason, setReason] = useState("");
  const [verify, setVerify] = useState<EvidenceCheck | null>(null);
  if (error) return <ErrorBox message={error} />;
  if (!a) return <Loading />;
  const doc = a.config as { versions: { version: number; created_at: string; config: Record<string, unknown> }[]; deviations: Record<string, unknown>[] };
  const latest = doc.versions[doc.versions.length - 1];
  const generate = async () => {
    setBusy(true); setErr(null);
    try { setRep(await api.experimentReport(id)); reload(); } catch (e) { setErr((e as Error).message); } finally { setBusy(false); }
  };
  const revise = async () => {
    setErr(null);
    try { await api.reviseExperiment(id, JSON.parse(changes), reason || null); reload(); } catch (e) { setErr((e as Error).message); }
  };
  const m1 = rep?.results.metrics.find((m) => m.id === "M1");
  const comp = rep?.results.completeness;
  return (
    <div className="stack">
      <Panel title={<span className="row gap">Experiment <code>{a.id}</code> <AnalysisStatus status={a.status} /></span>}
        sub={`${String(latest.config.title)} · version ${latest.version} · ${String(latest.config.experiment_type)}${latest.config.preregistered ? " · preregistered" : ""}`}
        actions={<span className="row gap">
          <Link to="/experiments" className="btn">All experiments</Link>
          <button className="btn primary" disabled={busy} onClick={generate}>{busy ? "Generating…" : "Generate report from persisted data"}</button>
          <button className="btn" onClick={() => api.experimentExport(id).then((b) => downloadJson(`gauntlet-experiment-${id}.json`, b))}>Export reproducibility bundle</button>
          <button className="btn" onClick={() => api.verifyEvidence(id).then(setVerify).catch((e) => setErr((e as Error).message))}>Verify evidence</button>
        </span>}>
        {err && <ErrorBox message={err} />}
        {verify && (verify.ok
          ? <div className="note-box small">Evidence verified: all {verify.checked} artifacts of the latest report are present and unchanged.</div>
          : <div className="warn-box small">Evidence changed since the latest report: missing {JSON.stringify(verify.missing)}, modified {JSON.stringify(verify.modified)}.</div>)}
        {rep?.evidence_check_against_previous_report && !rep.evidence_check_against_previous_report.ok &&
          <div className="warn-box small">Evidence used by the previous report has changed: {JSON.stringify(rep.evidence_check_against_previous_report)}</div>}
        <details><summary className="small">Configuration versions ({doc.versions.length}) and deviations ({doc.deviations.length})</summary>
          <Json value={doc} /></details>
        <details><summary className="small">Revise configuration (creates a new version; a reason is required after preregistration or a report)</summary>
          <div className="form-grid">
            <label><span>Changes (JSON)</span><textarea rows={3} value={changes} onChange={(e) => setChanges(e.target.value)} /></label>
            <label><span>Deviation reason</span><input value={reason} onChange={(e) => setReason(e.target.value)} /></label>
            <div className="form-actions"><button className="btn" onClick={revise}>Save new version</button></div>
          </div></details>
      </Panel>
      {rep && comp && <>
        <Panel title="Completeness" sub={comp.status === "complete" ? "Every required cell has a conclusive run." : undefined}>
          <p><AnalysisStatus status={comp.status === "complete" ? "completed" : comp.status === "incomplete" ? "stopped" : "defined"} />{" "}
            <strong>{comp.status}</strong>{comp.cells_required !== undefined ? `: ${comp.cells_conclusive} / ${comp.cells_required} required cells conclusive` : ` (${comp.reason})`}</p>
          {(comp.missing_cells?.length ?? 0) > 0 && <p className="small">Missing: {JSON.stringify(comp.missing_cells)}</p>}
          {(comp.unresolved_cells?.length ?? 0) > 0 && <p className="small">Unresolved (only non-conclusive runs): {JSON.stringify(comp.unresolved_cells)}</p>}
        </Panel>
        <Panel title="Metrics" sub="Values computed only from the selected persisted runs; definitions shown with every value.">
          <div className="table-wrap"><table className="table">
            <thead><tr><th>Metric</th><th>Result</th><th>Unit</th><th>Definition</th><th>Interval</th></tr></thead>
            <tbody>{rep.results.metrics.map((m) => <MetricRow key={m.id} m={m} />)}</tbody></table></div>
          {m1 && <p className="small">Excluded: {(m1.excluded ?? []).length} · inconclusive: {m1.ids?.inconclusive.join(", ") || "none"} · infrastructure errors: {m1.ids?.infra_error.join(", ") || "none"} · missing: {m1.ids?.missing.join(", ") || "none"}</p>}
          {rep.results.paired.length > 0 && <>
            <h3>Paired comparisons (matched scenario and seed)</h3>
            <div className="table-wrap"><table className="table compact">
              <thead><tr><th>A</th><th>B</th><th>both pass</th><th>only A fails</th><th>only B fails</th><th>both fail</th><th>unmatched / non-conclusive</th></tr></thead>
              <tbody>{rep.results.paired.map((p) => <tr key={`${p.a}-${p.b}`}><td><code>{p.a}</code></td><td><code>{p.b}</code></td>
                <td>{p.both_pass}</td><td>{p.only_a_fails}</td><td>{p.only_b_fails}</td><td>{p.both_fail}</td><td>{p.unmatched_or_non_conclusive}</td></tr>)}</tbody></table></div></>}
          <p className="small muted">Manifest: {rep.manifest.artifacts.length} artifacts · {rep.manifest.runs_referenced_by_metrics} of {rep.manifest.runs_selected} selected runs referenced by metrics · report SHA-256 <code>{rep.markdown_sha256.slice(0, 16)}</code></p>
        </Panel>
        <Panel title="Report" actions={<a className="btn" href={`data:text/markdown;charset=utf-8,${encodeURIComponent(rep.markdown)}`} download={`gauntlet-experiment-${id}.md`}>Download .md</a>}>
          <Markdown source={rep.markdown} />
        </Panel>
      </>}
    </div>
  );
}
