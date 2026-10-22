import { useEffect, useMemo, useState } from "react";
import { api, RunSummary } from "../api";
import { ErrorBox, Hash, Link, Loading, Panel, StatusBadge, fmtTime, useAsync } from "../components/ui";

export function RunBenchmark() {
  const scenarios = useAsync(() => api.scenarios());
  const impls = useAsync(() => api.implementations());
  const [scenario, setScenario] = useState("");
  const [impl, setImpl] = useState("");
  const [seed, setSeed] = useState("1");
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [runs, setRuns] = useState<RunSummary[] | null>(null);
  const [runsError, setRunsError] = useState<string | null>(null);
  const [filter, setFilter] = useState("");

  useEffect(() => {
    if (scenarios.data && !scenario) setScenario(scenarios.data[0].id);
    if (impls.data && !impl) setImpl(impls.data[0].id);
  }, [scenarios.data, impls.data, scenario, impl]);

  // Poll the run list while anything is queued or running; status shown is
  // exactly what the backend reports (no client-side progress estimation).
  useEffect(() => {
    let alive = true;
    let timer: number | undefined;
    const load = () =>
      api.runs(300).then((r) => {
        if (!alive) return;
        setRuns(r);
        setRunsError(null);
        const busy = r.some((x) => x.status !== "completed");
        timer = window.setTimeout(load, busy ? 1500 : 10000);
      }).catch((e) => {
        if (!alive) return;
        setRunsError(e.message);
        timer = window.setTimeout(load, 5000);
      });
    load();
    return () => {
      alive = false;
      window.clearTimeout(timer);
    };
  }, []);

  const seedNum = Number(seed);
  const seedValid = /^\d+$/.test(seed) && seedNum >= 0 && seedNum <= 2 ** 31 - 1;
  const selectedImpl = impls.data?.find((i) => i.id === impl);
  const selectedScenario = scenarios.data?.find((s) => s.id === scenario);

  const submit = async () => {
    setSubmitting(true);
    setError(null);
    try {
      const r = await api.startRun({ scenario_id: scenario, impl_id: impl, seed: seedNum });
      setRuns((prev) => [{ id: r.id, status: "queued", scenario_id: scenario, impl_id: impl, seed: seedNum, created_at: new Date().toISOString(),
        impl_kind: selectedImpl?.kind ?? "", verdict: null, schedule_hash: null, trace_hash: null, outcome_hash: null, duration_ms: null,
        benchmark_id: null, metrics: null, failed_invariants: [], infra_error: null }, ...(prev ?? [])]);
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setSubmitting(false);
    }
  };

  const shown = useMemo(() => (runs ?? []).filter((r) =>
    !filter || `${r.id} ${r.impl_id} ${r.scenario_id} ${r.verdict}`.toLowerCase().includes(filter.toLowerCase())), [runs, filter]);

  if (scenarios.error || impls.error) return <ErrorBox message={(scenarios.error || impls.error)!} />;
  if (!scenarios.data || !impls.data) return <Loading />;

  return (
    <div className="stack">
      <Panel title="Run benchmark" sub="Runs execute one at a time on the backend against a fresh PostgreSQL database.">
        <div className="form-grid">
          <label>
            <span>Scenario</span>
            <select value={scenario} onChange={(e) => setScenario(e.target.value)}>
              {scenarios.data.map((s) => <option key={s.id} value={s.id}>{s.id}</option>)}
            </select>
          </label>
          <label>
            <span>Implementation</span>
            <select value={impl} onChange={(e) => setImpl(e.target.value)}>
              <optgroup label="Reference implementations">
                {impls.data.filter((i) => i.kind === "reference").map((i) => <option key={i.id} value={i.id}>{i.id}</option>)}
              </optgroup>
              <optgroup label="Mutants (deliberately faulty)">
                {impls.data.filter((i) => i.kind === "mutant").map((i) => <option key={i.id} value={i.id}>{i.id}</option>)}
              </optgroup>
            </select>
          </label>
          <label>
            <span>Seed</span>
            <input value={seed} onChange={(e) => setSeed(e.target.value.trim())} inputMode="numeric" aria-invalid={!seedValid} />
          </label>
          <div className="form-actions">
            <button className="btn primary" disabled={!seedValid || submitting || !scenario || !impl} onClick={submit}>
              {submitting ? "Submitting…" : "Start run"}
            </button>
          </div>
        </div>
        {!seedValid && <p className="field-error">Seed must be an integer between 0 and 2147483647.</p>}
        {error && <ErrorBox message={error} />}
        <div className="two-col">
          {selectedScenario && (
            <div className="note-box">
              <strong>{selectedScenario.title}</strong>
              <p>{selectedScenario.purpose}</p>
              <p className="muted small">Faults: {selectedScenario.faults.join(", ") || "none"}</p>
            </div>
          )}
          {selectedImpl && (
            <div className="note-box">
              <strong>{selectedImpl.title}</strong>
              <p>{selectedImpl.description}</p>
              {selectedImpl.kind === "mutant" && (
                <p className="muted small">Mutation: {selectedImpl.mutation} · intended invariant <code>{selectedImpl.intended_invariant}</code></p>
              )}
            </div>
          )}
        </div>
      </Panel>

      <Panel title="Runs" sub="Persisted runs, newest first. Select two completed runs to compare."
        actions={<input className="search" placeholder="Filter…" value={filter} onChange={(e) => setFilter(e.target.value)} aria-label="Filter runs" />}>
        {runsError && <ErrorBox message={runsError} />}
        {!runs ? <Loading /> : <RunTable runs={shown} />}
      </Panel>
    </div>
  );
}

export function RunTable({ runs }: { runs: RunSummary[] }) {
  const [pick, setPick] = useState<string[]>([]);
  const toggle = (id: string) => setPick((p) => (p.includes(id) ? p.filter((x) => x !== id) : [...p, id].slice(-2)));
  return (
    <>
      <div className="row gap compare-bar">
        <span className="muted small">{pick.length}/2 selected</span>
        {pick.length === 2 ? <Link to={`/compare?a=${pick[0]}&b=${pick[1]}`} className="btn">Compare selected</Link> :
          <button className="btn" disabled>Compare selected</button>}
      </div>
      <div className="table-wrap">
        <table className="table">
          <thead>
            <tr>
              <th aria-label="select"></th><th>Run</th><th>Status</th><th>Implementation</th><th>Scenario</th><th>Seed</th>
              <th>Failed invariants</th><th>Schedule</th><th>Trace</th><th>Duration</th><th>Created</th>
            </tr>
          </thead>
          <tbody>
            {runs.map((r) => (
              <tr key={r.id}>
                <td>
                  <input type="checkbox" checked={pick.includes(r.id)} disabled={r.status !== "completed" || !r.trace_hash}
                    onChange={() => toggle(r.id)} aria-label={`select run ${r.id}`} />
                </td>
                <td><Link to={`/runs/${r.id}`}><code>{r.id}</code></Link></td>
                <td><StatusBadge status={r.status === "completed" ? r.verdict : r.status} /></td>
                <td className="mono">{r.impl_id}</td>
                <td className="mono">{r.scenario_id}</td>
                <td>{r.seed}</td>
                <td className="mono small">{r.failed_invariants.join(", ") || (r.infra_error ? <span title={r.infra_error}>infra error</span> : "–")}</td>
                <td><Hash value={r.schedule_hash} /></td>
                <td><Hash value={r.trace_hash} /></td>
                <td>{r.duration_ms != null ? `${(r.duration_ms / 1000).toFixed(1)}s` : "–"}</td>
                <td className="small">{fmtTime(r.created_at)}</td>
              </tr>
            ))}
            {runs.length === 0 && <tr><td colSpan={11} className="muted">No runs.</td></tr>}
          </tbody>
        </table>
      </div>
    </>
  );
}
