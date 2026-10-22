import { useState } from "react";
import { api, RunSummary } from "../api";
import { AnalysisList, AnalysisStatus, downloadJson, usePolled } from "../components/analysis";
import { ErrorBox, Json, Link, Loading, Panel, StatusBadge, useAsync } from "../components/ui";
import { ReplayStatus } from "./Differential";

export function MinimizePage({ id, go, runParam }: { id: string; go: (id: string) => void; runParam: string }) {
  return id ? <MinimizationDetail id={id} /> : <MinimizationStart go={go} runParam={runParam} />;
}

function MinimizationStart({ go, runParam }: { go: (id: string) => void; runParam: string }) {
  const runs = useAsync(() => api.runs(1000));
  const [runId, setRunId] = useState(runParam);
  const [invariant, setInvariant] = useState("");
  const [maxAttempts, setMaxAttempts] = useState("60");
  const [maxDuration, setMaxDuration] = useState("3600");
  const [repeats, setRepeats] = useState("1");
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  if (runs.error) return <ErrorBox message={runs.error} />;
  if (!runs.data) return <Loading />;
  const failing = runs.data.filter((r: RunSummary) => r.status === "completed" && r.verdict === "fail");
  const sel = failing.find((r) => r.id === runId);
  const ok = !!sel && [maxAttempts, maxDuration, repeats].every((v) => /^\d+$/.test(v) && Number(v) > 0);
  const start = async () => {
    if (busy) return;
    setBusy(true);
    setError(null);
    try {
      const m = await api.startMinimization({ run_id: runId, invariant: invariant || undefined,
        budget: { max_attempts: Number(maxAttempts), max_duration_s: Number(maxDuration), confirm_repeats: Number(repeats), reproduce_repeats: Number(repeats) } });
      go(m.id);
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setBusy(false);
    }
  };
  return (
    <div className="stack">
      <Panel title="Automatic failure minimization" sub="Delta debugging over removable schedule units. Only reductions that reproduce the same failure signature (target invariant, diagnosis category, affected orders) are kept; the original run is never modified. The result is minimal only with respect to the attempted unit deletions.">
        <div className="form-grid">
          <label><span>Failing run ({failing.length})</span>
            <select value={runId} onChange={(e) => { setRunId(e.target.value); setInvariant(""); }}>
              <option value="">select…</option>
              {failing.map((r) => <option key={r.id} value={r.id}>{r.id} · {r.impl_id} · {r.scenario_id.split("_")[0]} · seed {r.seed} · {r.failed_invariants.join(",")}</option>)}
            </select></label>
          <label><span>Target invariant</span>
            <select value={invariant} onChange={(e) => setInvariant(e.target.value)} disabled={!sel}>
              <option value="">first failed</option>
              {sel?.failed_invariants.map((i) => <option key={i} value={i}>{i}</option>)}
            </select></label>
          <label><span>Max executions</span><input value={maxAttempts} onChange={(e) => setMaxAttempts(e.target.value.trim())} inputMode="numeric" /></label>
          <label><span>Max duration (s)</span><input value={maxDuration} onChange={(e) => setMaxDuration(e.target.value.trim())} inputMode="numeric" /></label>
          <label><span>Repetitions (flakiness)</span><input value={repeats} onChange={(e) => setRepeats(e.target.value.trim())} inputMode="numeric" /></label>
          <div className="form-actions"><button className="btn primary" disabled={!ok || busy} onClick={start}>{busy ? "Starting…" : "Minimize"}</button></div>
        </div>
        {error && <ErrorBox message={error} />}
      </Panel>
      <Panel title="Minimizations"><AnalysisList kind="minimization" base="/minimize"
        describe={(a) => `run ${a.config.run_id} · ${a.config.invariant} · budget ${a.config.budget?.max_attempts} executions`} /></Panel>
    </div>
  );
}

function MinimizationDetail({ id }: { id: string }) {
  const { data: a, error } = usePolled(() => api.minimization(id), id);
  const [replay, setReplay] = useState<string | null>(null);
  if (error) return <ErrorBox message={error} />;
  if (!a) return <Loading />;
  const r = a.result;
  const running = a.status === "queued" || a.status === "running";
  const m = r?.minimized;
  return (
    <div className="stack">
      <Panel title={<span className="row gap">Minimization <code>{a.id}</code> <AnalysisStatus status={a.status} /></span>}
        sub={`run ${a.config.run_id} · target ${a.config.invariant} · ${a.config.impl_id ?? ""} × ${a.config.scenario_id ?? ""}`}
        actions={<span className="row gap">
          <Link to="/minimize" className="btn">New minimization</Link>
          {running && <button className="btn" onClick={() => api.cancelAnalysis(a.id)}>Cancel</button>}
          {r && <button className="btn" onClick={() => downloadJson(`gauntlet-minimization-${a.id}.json`, a)}>Export JSON</button>}
        </span>}>
        {!r && <p className="muted">The backend reports this minimization as <strong>{a.status}</strong>.</p>}
        {r && <>
          <p>{r.minimality ?? "in progress"}{r.stop_reason ? ` · stopped: ${r.stop_reason}` : ""} · {r.attempts_used} candidate executions in {r.elapsed_s}s ·
            original run unchanged: <strong>{r.original_unchanged === undefined ? "not yet checked" : String(r.original_unchanged)}</strong></p>
          {r.note && <div className="note-box small">{r.note}</div>}
          {r.reproduction && <p className="small">Original reproduced before reduction: {r.reproduction.map((x) => `${x.run_id}: ${x.match ? "yes" : "no"}`).join(", ")}</p>}
        </>}
      </Panel>
      {r?.reduction && m && <>
        <Panel title="Original vs minimized"
          actions={<Link to={`/compare?a=${r.original.run_id}&b=${m.run_id}`} className="btn">Compare traces</Link>}>
          <table className="table compact"><thead><tr><th>Size</th><th>Original</th><th>Minimized</th></tr></thead>
            <tbody>{Object.entries(r.reduction).map(([k, [o, n]]) => <tr key={k}><td>{k.replace(/_/g, " ")}</td><td>{o}</td><td>{n}</td></tr>)}</tbody></table>
          <div className="two-col">
            <div><strong>Original</strong> <Link to={`/runs/${r.original.run_id}`}><code>{r.original.run_id}</code></Link><Json value={r.original.signature} /></div>
            <div><strong>Minimized</strong> <Link to={`/runs/${m.run_id}`}><code>{m.run_id}</code></Link><Json value={m.signature} /></div>
          </div>
          <p className="small">{m.replay
            ? <>Independent replay of the minimized schedule: run <Link to={`/runs/${m.replay.run_id}`}><code>{m.replay.run_id}</code></Link> ·
              signature match <strong>{String(m.replay.signature_match)}</strong> · same trace {String(m.replay.same_trace_as_accepted)} · same outcome {String(m.replay.same_outcome_as_accepted)}</>
            : <strong>{m.replay_note ?? "Not replayed."}</strong>}{" "}
            <button className="btn small" onClick={() => api.replayRun(m.run_id).then((x) => setReplay(x.id))}>Replay again</button>
            <button className="btn small" onClick={() => downloadJson(`gauntlet-minimized-${a.id}.json`, m.schedule)}>Export minimized schedule</button></p>
          {replay && <ReplayStatus id={replay} />}
        </Panel>
      </>}
      {r && (
        <Panel title={`Retained units (${r.retained_units.length}) and removed units (${r.removed_units.length} of ${r.units_total})`}>
          <div className="two-col">
            <ul className="small">{r.retained_units.map((u) => <li key={u.id}><code>{u.id}</code> {u.desc}</li>)}</ul>
            <details><summary className="small">Removed units</summary><ul className="small">{r.removed_units.map((u) => <li key={u.id}><code>{u.id}</code> {u.desc}</li>)}</ul></details>
          </div>
        </Panel>)}
      {r && (
        <Panel title={`Reduction attempts (${r.attempts.length})`} sub="Accepted only when the target failure signature is reproduced; errors, timeouts and unrelated failures are rejected.">
          <div className="table-wrap"><table className="table compact">
            <thead><tr><th>#</th><th>Phase</th><th>Units kept</th><th>Result</th><th>Reason</th><th>Failed invariants</th><th>Run</th></tr></thead>
            <tbody>{r.attempts.map((x) => (
              <tr key={x.n} className={x.result === "accepted" ? "row-diff" : ""}>
                <td>{x.n}</td><td className="small">{x.phase}</td><td>{x.kept_units}</td>
                <td><StatusBadge status={x.result === "accepted" ? "pass" : x.result === "invalid" ? "not_applicable" : "fail"} compact /> {x.result}</td>
                <td className="small">{x.reason}</td><td className="mono small">{(x.failed_invariants ?? []).join(", ") || "–"}</td>
                <td>{(x.run_ids ?? []).map((id) => <Link key={id} to={`/runs/${id}`}><code>{id}</code></Link>)}</td>
              </tr>))}</tbody>
          </table></div>
        </Panel>)}
    </div>
  );
}
