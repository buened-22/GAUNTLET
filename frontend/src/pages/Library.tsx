import { useState } from "react";
import { api } from "../api";
import { ErrorBox, Json, Loading, Panel, useAsync } from "../components/ui";

export function Library() {
  const scenarios = useAsync(() => api.scenarios());
  const impls = useAsync(() => api.implementations());
  const invariants = useAsync(() => api.invariants());
  const faults = useAsync(() => api.faults());
  const [preview, setPreview] = useState<{ id: string; seed: number } | null>(null);
  const err = scenarios.error || impls.error || invariants.error || faults.error;
  if (err) return <ErrorBox message={err} />;
  if (!scenarios.data || !impls.data || !invariants.data || !faults.data) return <Loading />;
  return (
    <div className="stack">
      <Panel title="Scenarios" sub="Each scenario drives the full P0→P5 migration and concentrates adversarial pressure on one part of the protocol.">
        <div className="cards">
          {scenarios.data.map((s) => (
            <article key={s.id} className="card">
              <h3><code>{s.id}</code></h3>
              <div className="card-title">{s.title}</div>
              <p>{s.purpose}</p>
              <dl>
                <dt>Preconditions</dt><dd><ul>{s.preconditions.map((p) => <li key={p}>{p}</li>)}</ul></dd>
                <dt>Faults</dt><dd>{s.faults.join(", ") || "none"}</dd>
                <dt>Expected behaviour</dt><dd>{s.expected_behavior}</dd>
              </dl>
              <button className="btn small" onClick={() => setPreview({ id: s.id, seed: 1 })}>View schedule (seed 1)</button>
            </article>
          ))}
        </div>
        {preview && <SchedulePreview key={preview.id} id={preview.id} seed={preview.seed} onClose={() => setPreview(null)} />}
      </Panel>

      <Panel title="Implementations" sub="Reference implementations and deliberately faulty mutants. Each mutant overrides exactly one method of its base.">
        <div className="table-wrap">
          <table className="table">
            <thead><tr><th>Id</th><th>Kind</th><th>Strategy</th><th>Description</th><th>Mutation</th><th>Intended invariant</th><th>Intended scenarios</th></tr></thead>
            <tbody>
              {impls.data.map((i) => (
                <tr key={i.id}>
                  <td className="mono">{i.id}</td><td>{i.kind}</td><td>{i.strategy}</td>
                  <td className="small">{i.title}. {i.description}</td>
                  <td className="small">{i.mutation ?? "–"}</td>
                  <td className="mono small">{i.intended_invariant ?? "–"}</td>
                  <td className="mono small">{i.intended_scenarios.join(", ") || "–"}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </Panel>

      <Panel title="Invariants">
        <div className="table-wrap">
          <table className="table">
            <thead><tr><th>Id</th><th>Category</th><th>Statement</th><th>Verification procedure</th></tr></thead>
            <tbody>
              {invariants.data.map((i) => (
                <tr key={i.id}><td className="mono">{i.id}<div className="small">{i.title}</div></td><td>{i.category}</td><td className="small">{i.statement}</td><td className="small">{i.procedure}</td></tr>
              ))}
            </tbody>
          </table>
        </div>
      </Panel>

      <Panel title="Fault catalogue">
        <div className="table-wrap">
          <table className="table">
            <thead><tr><th>Id</th><th>Fault</th><th>Preconditions</th><th>Injection point</th><th>Expected behaviour</th><th>Recovery obligation</th></tr></thead>
            <tbody>
              {faults.data.map((f) => (
                <tr key={f.id}><td className="mono">{f.id}</td><td className="small">{f.title}</td><td className="small">{f.preconditions}</td>
                  <td className="small">{f.injection_point}</td><td className="small">{f.expected_behavior} <span className="muted">{f.expected_invariants}</span></td><td className="small">{f.recovery_obligation}</td></tr>
              ))}
            </tbody>
          </table>
        </div>
      </Panel>
    </div>
  );
}

function SchedulePreview({ id, seed, onClose }: { id: string; seed: number; onClose: () => void }) {
  const [s, setS] = useState(seed);
  const p = useAsync(() => api.scenario(id, s), [id, s]);
  return (
    <div className="preview">
      <div className="row gap">
        <strong>Schedule definition: <code>{id}</code></strong>
        <label className="row gap small">seed <input type="number" min={0} value={s} onChange={(e) => setS(Math.max(0, Number(e.target.value) || 0))} style={{ width: 90 }} /></label>
        <button className="btn small" onClick={onClose}>Close</button>
      </div>
      {p.error && <ErrorBox message={p.error} />}
      {p.data && (
        <>
          <p className="small muted">schedule hash <code>{p.data.schedule_preview.schedule_hash.slice(0, 16)}</code> · {p.data.schedule_preview.directives.length} directives ·
            {" "}{Object.keys(p.data.schedule_preview.initial_orders).length} initial orders. This is a pure function of scenario, seed and benchmark version.</p>
          <ol className="directives">
            {p.data.schedule_preview.directives.map((d, i) => (
              <li key={i}><code>{String(d.kind)}</code> <span className="mono small">{JSON.stringify(Object.fromEntries(Object.entries(d).filter(([k]) => k !== "kind")))}</span></li>
            ))}
          </ol>
          <details><summary className="small">Initial orders</summary><Json value={p.data.schedule_preview.initial_orders} /></details>
        </>
      )}
    </div>
  );
}
