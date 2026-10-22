import { useEffect, useState } from "react";
import { api, Comparison, RunSummary } from "../api";
import { ErrorBox, Hash, Json, Link, Loading, Panel, StatusBadge } from "../components/ui";

export function Compare({ a, b, go }: { a: string; b: string; go: (a: string, b: string) => void }) {
  const [runs, setRuns] = useState<RunSummary[] | null>(null);
  const [cmp, setCmp] = useState<Comparison | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [selA, setSelA] = useState(a);
  const [selB, setSelB] = useState(b);

  useEffect(() => { api.runs(500).then((r) => setRuns(r.filter((x) => x.status === "completed" && x.trace_hash))).catch((e) => setError(e.message)); }, []);
  useEffect(() => {
    setCmp(null);
    setError(null);
    if (a && b) api.compare(a, b).then(setCmp).catch((e) => setError(e.message));
  }, [a, b]);

  const label = (r: RunSummary) => `${r.id} · ${r.impl_id} · ${r.scenario_id.split("_")[0]} · seed ${r.seed} · ${r.verdict}`;
  return (
    <div className="stack">
      <Panel title="Run comparison" sub="Compare two completed runs by scenario, seed, logical trace and invariant outcomes.">
        {!runs ? <Loading /> : (
          <div className="form-grid">
            <label><span>Run A</span>
              <select value={selA} onChange={(e) => setSelA(e.target.value)}>
                <option value="">select…</option>
                {runs.map((r) => <option key={r.id} value={r.id}>{label(r)}</option>)}
              </select></label>
            <label><span>Run B</span>
              <select value={selB} onChange={(e) => setSelB(e.target.value)}>
                <option value="">select…</option>
                {runs.map((r) => <option key={r.id} value={r.id}>{label(r)}</option>)}
              </select></label>
            <div className="form-actions"><button className="btn primary" disabled={!selA || !selB || selA === selB} onClick={() => go(selA, selB)}>Compare</button></div>
          </div>
        )}
      </Panel>
      {error && <ErrorBox message={error} />}
      {a && b && !cmp && !error && <Loading what="Comparing" />}
      {cmp && <ComparisonView c={cmp} />}
    </div>
  );
}

const yn = (v: boolean) => <span className={v ? "" : "txt-crit"}>{v ? "same" : "different"}</span>;

function ComparisonView({ c }: { c: Comparison }) {
  const side = (s: Record<string, unknown>) => (
    <div className="note-box">
      <div className="row gap"><StatusBadge status={s.verdict as string} /><Link to={`/runs/${s.id}`}><code>{String(s.id)}</code></Link></div>
      <div className="mono small">{String(s.impl_id)} ({String(s.impl_kind)}) × {String(s.scenario_id)} · seed {String(s.seed)}</div>
      <div className="small">schedule <Hash value={s.schedule_hash as string} /> trace <Hash value={s.trace_hash as string} /> outcome <Hash value={s.outcome_hash as string} /></div>
    </div>
  );
  const kinds = Array.from(new Set([...Object.keys(c.event_kinds.a), ...Object.keys(c.event_kinds.b)])).sort();
  return (
    <div className="stack">
      <Panel title="Configuration">
        <div className="two-col">{side(c.a)}{side(c.b)}</div>
        <table className="table compact">
          <tbody>
            <tr><th>Implementation</th><td>{yn(c.same_implementation)} <span className="mono small">({String(c.a.impl_id)} / {String(c.b.impl_id)})</span></td>
              <th>Benchmark version</th><td>{yn(c.same_benchmark_version)} <span className="mono small">({String(c.a.benchmark_version ?? "?")} / {String(c.b.benchmark_version ?? "?")})</span></td></tr>
            <tr><th>Scenario</th><td>{yn(c.same_scenario)}</td><th>Seed</th><td>{yn(c.same_seed)}</td></tr>
            <tr><th>Logical schedule</th><td>{yn(c.same_schedule)}</td><th>Statement-level trace</th><td>{yn(c.same_trace)}</td></tr>
            <tr><th>Outcome</th><td>{yn(c.same_outcome)}</td><th>First divergence</th><td>{c.first_divergence ? `event index ${c.first_divergence.index}` : "none"}</td></tr>
          </tbody>
        </table>
      </Panel>
      <Panel title="Invariant outcomes">
        <table className="table">
          <thead><tr><th>Invariant</th><th>Run A</th><th>Run B</th></tr></thead>
          <tbody>
            {c.invariants.map((i) => (
              <tr key={i.id} className={i.differs ? "row-diff" : ""}>
                <td className="mono">{i.id}{i.differs && <span className="tag">differs</span>}</td>
                <td><StatusBadge status={i.a} /></td><td><StatusBadge status={i.b} /></td>
              </tr>
            ))}
          </tbody>
        </table>
      </Panel>
      <div className="split">
        <Panel title="Event counts by kind">
          <table className="table compact">
            <thead><tr><th>Kind</th><th>A</th><th>B</th></tr></thead>
            <tbody>{kinds.map((k) => <tr key={k} className={c.event_kinds.a[k] !== c.event_kinds.b[k] ? "row-diff" : ""}><td className="mono">{k}</td><td>{c.event_kinds.a[k] ?? 0}</td><td>{c.event_kinds.b[k] ?? 0}</td></tr>)}</tbody>
          </table>
        </Panel>
        <Panel title="First divergence in the logical trace">
          {c.first_divergence ? <div className="two-col"><Json value={c.first_divergence.a} /><Json value={c.first_divergence.b} /></div> : <p className="muted">The logical traces are identical.</p>}
        </Panel>
      </div>
      <Panel title={`Outcome differences (${c.outcome_differences_total ?? c.outcome_differences.length})`}
        sub={(c.outcome_differences_total ?? 0) > c.outcome_differences.length
          ? `Attempts whose client-visible outcome differs; the first ${c.outcome_differences.length} are listed.`
          : "Attempts whose client-visible outcome differs."}>
        {c.outcome_differences.length === 0 ? <p className="muted">None.</p> : (
          <div className="table-wrap tall">
            <table className="table">
              <thead><tr><th>Attempt</th><th>Op</th><th>A</th><th>B</th></tr></thead>
              <tbody>{c.outcome_differences.map((d) => <tr key={d.attempt_id}><td className="mono">{d.attempt_id}</td><td className="mono">{d.op}</td><td className="mono small">{JSON.stringify(d.a)}</td><td className="mono small">{JSON.stringify(d.b)}</td></tr>)}</tbody>
            </table>
          </div>
        )}
      </Panel>
      <Panel title={`Final state differences (${c.final_state_differences.length})`}>
        {c.final_state_differences.length === 0 ? <p className="muted">None.</p> : (
          <table className="table compact">
            <thead><tr><th>Order</th><th>A</th><th>B</th></tr></thead>
            <tbody>{c.final_state_differences.map((d) => <tr key={d.id}><td className="mono">{d.id}</td><td className="mono small">{JSON.stringify(d.a)}</td><td className="mono small">{JSON.stringify(d.b)}</td></tr>)}</tbody>
          </table>
        )}
      </Panel>
    </div>
  );
}
