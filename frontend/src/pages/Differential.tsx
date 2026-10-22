import { useState } from "react";
import { api, DiffPair, DifferentialResult } from "../api";
import { AnalysisList, AnalysisStatus, downloadJson, usePolled } from "../components/analysis";
import { ErrorBox, Hash, Json, Link, Loading, Panel, StatusBadge, useAsync } from "../components/ui";

const CLASS_META: Record<DifferentialResult["classification"], { icon: string; cls: string; label: string }> = {
  agreement: { icon: "=", cls: "st-pass", label: "Agreement" },
  confirmed_violation: { icon: "✕", cls: "st-fail", label: "Confirmed violation" },
  unconfirmed_disagreement: { icon: "≠", cls: "st-inc", label: "Unconfirmed disagreement" },
  inconclusive: { icon: "?", cls: "st-infra", label: "Inconclusive" },
};

export function Classification({ c }: { c: DifferentialResult["classification"] }) {
  const m = CLASS_META[c];
  return <span className={`badge ${m.cls}`}><span aria-hidden="true">{m.icon}</span><span>{m.label}</span></span>;
}

export function DifferentialPage({ id, go }: { id: string; go: (id: string) => void }) {
  return id ? <DifferentialDetail id={id} /> : <DifferentialStart go={go} />;
}

function DifferentialStart({ go }: { go: (id: string) => void }) {
  const scenarios = useAsync(() => Promise.all([api.scenarios(), api.fixtures()]).then(([a, b]) => [...a, ...b]));
  const impls = useAsync(() => api.implementations());
  const rules = useAsync(() => api.differentialRules());
  const [scenario, setScenario] = useState("S02_backfill_vs_legacy_writes");
  const [seed, setSeed] = useState("1");
  const [picked, setPicked] = useState<string[]>(["ref_a_trigger", "ref_b_dualwrite"]);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  if (scenarios.error || impls.error) return <ErrorBox message={(scenarios.error || impls.error)!} />;
  if (!scenarios.data || !impls.data) return <Loading />;
  const seedOk = /^\d+$/.test(seed);
  const toggle = (i: string) => setPicked((p) => (p.includes(i) ? p.filter((x) => x !== i) : [...p, i]));
  const start = async () => {
    setBusy(true);
    setError(null);
    try {
      const r = await api.startDifferential({ impl_ids: picked, scenario_id: scenario, seed: Number(seed) });
      go(r.id);
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setBusy(false);
    }
  };
  return (
    <div className="stack">
      <Panel title="Differential testing" sub="Run two or more implementations on the same logical schedule, each on its own fresh database, and compare normalized client-visible behaviour. The independent oracle decides whether a disagreement is a confirmed violation.">
        <div className="form-grid">
          <label><span>Scenario</span>
            <select value={scenario} onChange={(e) => setScenario(e.target.value)}>
              {scenarios.data.map((s) => <option key={s.id} value={s.id}>{s.id}{(s as { fixture?: boolean }).fixture ? " (fixture)" : ""}</option>)}
            </select></label>
          <label><span>Seed</span><input value={seed} onChange={(e) => setSeed(e.target.value.trim())} inputMode="numeric" aria-invalid={!seedOk} /></label>
          <div className="form-actions">
            <button className="btn primary" disabled={busy || picked.length < 2 || !seedOk} onClick={start}>{busy ? "Starting…" : "Start comparison"}</button>
          </div>
        </div>
        <fieldset className="checks">
          <legend className="small">Implementations (choose at least two)</legend>
          {impls.data.map((i) => (
            <label key={i.id} className="row gap small">
              <input type="checkbox" checked={picked.includes(i.id)} onChange={() => toggle(i.id)} />
              <code>{i.id}</code> <span className="muted">({i.kind}, strategy {i.strategy})</span>
            </label>))}
        </fieldset>
        {picked.length < 2 && <p className="field-error">Choose at least two implementations.</p>}
        {error && <ErrorBox message={error} />}
      </Panel>
      {rules.data && (
        <Panel title="Comparison rules" sub="Normalization never removes acknowledgements, client results, records or amounts.">
          <ul className="small">{rules.data.normalization_rules.map((r) => <li key={r.id}><strong>{r.id}</strong> {r.rule} <span className="muted">{r.why}</span></li>)}</ul>
          <ul className="small">{Object.entries(rules.data.classifications).map(([k, v]) => <li key={k}><strong>{k}</strong>: {v}</li>)}</ul>
        </Panel>
      )}
      <Panel title="Comparisons"><AnalysisList kind="differential" base="/differential"
        describe={(a) => `${(a.config.impl_ids ?? []).join(" vs ")} × ${a.config.scenario_id} · seed ${a.config.seed}`} /></Panel>
    </div>
  );
}

function DifferentialDetail({ id }: { id: string }) {
  const { data: a, error } = usePolled(() => api.differential(id), id);
  const [replayId, setReplayId] = useState<string | null>(null);
  if (error) return <ErrorBox message={error} />;
  if (!a) return <Loading />;
  const r = a.result;
  return (
    <div className="stack">
      <Panel title={<span className="row gap">Comparison <code>{a.id}</code> <AnalysisStatus status={a.status} /> {r && <Classification c={r.classification} />}</span>}
        sub={`${(a.config.impl_ids ?? []).join(" vs ")} × ${a.config.scenario_id} · seed ${a.config.seed}`}
        actions={<span className="row gap">
          <Link to="/differential" className="btn">New comparison</Link>
          {a.status === "completed" && <button className="btn" onClick={() => api.replayDifferential(a.id).then((x) => setReplayId(x.id))}>Replay comparison</button>}
          {r && <button className="btn" onClick={() => downloadJson(`gauntlet-comparison-${a.id}.json`, a)}>Export JSON</button>}
        </span>}>
        {!r && <p className="muted">The backend reports this comparison as <strong>{a.status}</strong>. Results appear only after it completes.</p>}
        {r && <p><strong>Classification:</strong> {r.reason}</p>}
        {r?.shared_violations && <div className="warn-box">Agreement does not imply correctness: the oracle FAILs invariants in these runs: <code>{JSON.stringify(r.shared_violations)}</code></div>}
        {replayId && <ReplayStatus id={replayId} />}
        {r?.replay_check && <p className="small">This comparison is a replay of <Link to={`/differential/${r.replay_check.replay_of}`}><code>{r.replay_check.replay_of}</code></Link>: reproduced = <strong>{String(r.replay_check.reproduced)}</strong></p>}
      </Panel>
      {r && <>
        <Panel title="Configuration" sub="Every implementation executed the same schedule document on its own newly created database.">
          <div className="table-wrap"><table className="table">
            <thead><tr><th>Implementation</th><th>Kind / strategy</th><th>Source SHA-256</th><th>Run</th><th>Oracle verdict</th><th>Failed invariants</th><th>Database</th></tr></thead>
            <tbody>{r.runs.map((x) => (
              <tr key={x.id}>
                <td><code>{x.impl_id}</code></td>
                <td className="small">{x.impl_kind}, strategy {x.strategy}</td>
                <td><Hash value={x.provenance?.implementation.source_sha256} /></td>
                <td><Link to={`/runs/${x.id}`}><code>{x.id}</code></Link></td>
                <td><StatusBadge status={x.verdict} />{x.error_class && <span className="small"> ({x.error_class})</span>}</td>
                <td className="mono small">{x.failed_invariants.join(", ") || "–"}</td>
                <td className="mono small">{x.database ?? "–"}</td>
              </tr>))}</tbody>
          </table></div>
          <table className="table compact"><tbody>
            <tr><th>Scenario</th><td><code>{a.config.scenario_id}</code></td><th>Seed</th><td>{a.config.seed}</td></tr>
            <tr><th>Schedule</th><td><Hash value={a.config.schedule_id} /> ({a.config.schedule_source})</td><th>Initial state</th><td><Hash value={a.config.initial_state_fingerprint} /></td></tr>
            <tr><th>Same schedule hash</th><td>{yn(r.equivalence.same_schedule_hash)}</td><th>Same initial state</th><td>{yn(r.equivalence.same_initial_state_fingerprint)}</td></tr>
            <tr><th>Isolated databases</th><td>{yn(r.equivalence.isolated_databases)}</td><th>Oracle version</th><td><Hash value={r.runs[0]?.provenance?.oracle_fingerprint} /></td></tr>
          </tbody></table>
          <p className="small muted">Equivalent logical input is not identical physical execution: statement-level interleavings are chosen among runnable actors, so implementations that issue different statements can legitimately see different interleavings.</p>
        </Panel>
        {r.pairs.map((p) => <PairView key={`${p.a}-${p.b}`} p={p} r={r} />)}
      </>}
    </div>
  );
}

const yn = (v: boolean) => <span className={v ? "" : "txt-crit"}>{v ? "yes" : "NO"}</span>;

function PairView({ p, r }: { p: DiffPair; r: DifferentialResult }) {
  const impl = (rid: string) => r.runs.find((x) => x.id === rid)?.impl_id ?? rid;
  const fd = p.first_meaningful_divergence;
  return (
    <Panel title={<span><code>{impl(p.a)}</code> vs <code>{impl(p.b)}</code></span>}
      sub={p.differs ? "Normalized behaviour differs." : "No normalized difference."}
      actions={<Link to={`/compare?a=${p.a}&b=${p.b}`} className="btn">Open in Compare runs</Link>}>
      {fd && <>
        <h3>Earliest meaningful divergence: attempt <code>{fd.attempt_id}</code></h3>
        <p className="small muted">An event that precedes a divergence is context, not a demonstrated cause.</p>
        <div className="two-col">
          <div><strong>{impl(p.a)}</strong> (completed at seq {fd.a_complete_seq ?? "–"})<Json value={fd.a} /><details><summary className="small">Preceding logical events</summary><Json value={fd.a_context} /></details></div>
          <div><strong>{impl(p.b)}</strong> (completed at seq {fd.b_complete_seq ?? "–"})<Json value={fd.b} /><details><summary className="small">Preceding logical events</summary><Json value={fd.b_context} /></details></div>
        </div>
      </>}
      <h3>Invariant findings</h3>
      <table className="table compact"><thead><tr><th>Implementation</th><th>Verdict</th><th>FAIL</th><th>Inconclusive</th></tr></thead>
        <tbody>{[p.a, p.b].map((rid) => { const o = r.oracle[impl(rid)]; return (
          <tr key={rid}><td><code>{impl(rid)}</code></td><td><StatusBadge status={o.verdict} /></td>
            <td className="mono small">{o.failed_invariants.join(", ") || "–"}</td><td className="mono small">{o.inconclusive_invariants.join(", ") || "–"}</td></tr>); })}
        </tbody></table>
      <h3>Client-visible attempt differences ({p.attempt_differences_total})</h3>
      {p.attempt_differences.length === 0 ? <p className="muted">None.</p> : (
        <div className="table-wrap"><table className="table compact"><thead><tr><th>Attempt</th><th>{impl(p.a)}</th><th>{impl(p.b)}</th></tr></thead>
          <tbody>{p.attempt_differences.map((d) => <tr key={d.attempt_id}><td className="mono">{d.attempt_id}</td>
            <td className="mono small">{JSON.stringify(d.a)}</td><td className="mono small">{JSON.stringify(d.b)}</td></tr>)}</tbody></table></div>)}
      <h3>Final state ({p.final_columns.same ? "same columns" : "different columns"}, {p.final_row_differences.length} differing rows)</h3>
      {p.final_row_differences.length > 0 && <Json value={p.final_row_differences} />}
      {p.final_metadata && !p.final_metadata.same && <>
        <p className="txt-crit small">Final constraints / triggers / migration stage differ:</p>
        <div className="two-col"><Json value={p.final_metadata.a} /><Json value={p.final_metadata.b} /></div></>}
      <p className="small muted">First statement-level trace divergence: {p.first_statement_level_divergence_index ?? "none"} (statement-level differences between strategies are expected and are not classified on their own).</p>
    </Panel>
  );
}

/** Status of a replay job: a run replay (links the new run) or a comparison replay (links the new comparison). */
export function ReplayStatus({ id }: { id: string }) {
  const { data } = usePolled(() => api.replay(id), id);
  if (!data) return <p className="small muted">Replay queued…</p>;
  if (data.status === "queued" || data.status === "running") return <p className="small"><AnalysisStatus status={data.status} /> Replay {id}</p>;
  const res = data.result;
  const isComparison = !!res?.replay_of;
  return (
    <div className="note-box small" data-testid="replay-status">
      Replay <code>{id}</code>: <AnalysisStatus status={data.status} />{" "}
      {res && <>reproduced = <strong>{String(res.reproduced)}</strong>
        {res.replay_id && (isComparison
          ? <> · new comparison <Link to={`/differential/${res.replay_id}`}><code>{res.replay_id}</code></Link></>
          : <> · replay run <Link to={`/runs/${res.replay_id}`}><code>{res.replay_id}</code></Link></>)}
        {res.same && <> · {Object.entries(res.same).map(([k, v]) => `${k} ${v ? "same" : "DIFFERENT"}`).join(", ")}</>}
        {res.per_implementation && <> · {Object.entries(res.per_implementation).map(([impl, s]) =>
          `${impl}: ${Object.values(s).every(Boolean) ? "same" : "DIFFERENT"}`).join(", ")}</>}</>}
      {res?.stop_reason && <ErrorBox message={res.stop_reason} />}
    </div>
  );
}
