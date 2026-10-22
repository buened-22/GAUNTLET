import { useState } from "react";
import { api, Benchmark, MatrixCell } from "../api";
import { ErrorBox, Link, Loading, Panel, Stat, StatusBadge, fmtTime, shortScenario, useAsync } from "../components/ui";

export function Overview() {
  const meta = useAsync(() => api.meta());
  const health = useAsync(() => api.health());
  const bench = useAsync<Benchmark | null>(() => api.latestBenchmark().catch((e) => (e.status === 404 ? null : Promise.reject(e))));

  if (meta.error) return <ErrorBox message={meta.error} />;
  if (!meta.data) return <Loading />;
  const m = meta.data;
  const h = m.latest_benchmark;
  return (
    <div className="stack">
      <Panel title="Gauntlet" sub={`Benchmark version ${m.benchmark_version}`}>
        <p className="lede">
          Gauntlet asks whether software that passes conventional functional tests stays correct when the environment
          gets adversarial: concurrency, partial failure, retries and interrupted execution. This prototype has one task
          family. It migrates <code>orders.total NUMERIC(12,2)</code> to <code>orders.total_cents BIGINT</code> while
          legacy and new application versions share a PostgreSQL 16 database. Each actor runs in its own OS process
          behind statement-level gates, and the engine controls the order in which statements commit. An independent
          oracle checks the client-side ledger against raw database snapshots.
        </p>
        <div className="stat-row">
          <Stat label="Scenarios" value={m.counts.scenarios} />
          <Stat label="Implementations" value={m.counts.implementations} note={`${m.counts.references} reference · ${m.counts.mutants} mutants`} />
          <Stat label="Invariants" value={m.counts.invariants} />
          <Stat label="Fault types" value={m.counts.faults} />
          <Stat label="PostgreSQL" value={health.data ? (health.data.ok ? health.data.postgres?.server_version ?? "?" : "unreachable") : null}
            note={health.data && !health.data.ok ? health.data.error?.slice(0, 80) : undefined} />
        </div>
      </Panel>

      <Panel title="Latest measured results" sub={h ? `Benchmark ${h.benchmark_id} · finished ${fmtTime(h.finished_at)}` : undefined}
        actions={h && <Link to="/report" className="btn">Full report</Link>}>
        {!h ? (
          <p className="muted">
            No benchmark has been executed yet. Every metric here is <strong>unavailable</strong>, not zero. To produce
            one, run <code>python -m gauntlet bench</code>.
          </p>
        ) : (
          <>
            <div className="stat-row">
              <Stat label="Runs completed" value={h.runs_completed} note={`${h.scenarios_executed} scenarios × seeds ${h.seeds.join(", ")}`} />
              <Stat label="Invariant violations" value={h.invariant_violations} note="FAIL results across all runs" />
              <Stat label="Mutants detected" value={`${h.mutants_detected} / ${h.mutants_total}`} note={`${h.mutants_killed_as_intended} killed by their intended invariant`} />
              <Stat label="Inconclusive runs" value={h.inconclusive_runs} />
              <Stat label="Infrastructure failures" value={h.infrastructure_failures} />
              <Stat label="Reproduced" value={`${h.reproducibility.reproduced} / ${h.reproducibility.checked}`} note="identical schedule, trace, outcome and verdict on re-run" />
            </div>
            {bench.data?.summary && <Matrix bench={bench.data} />}
            {bench.data?.summary && <MutantTable bench={bench.data} />}
          </>
        )}
      </Panel>

      <Panel title="Latest run" actions={<Link to="/run" className="btn">Run benchmark</Link>}>
        {m.latest_run ? (
          <div className="row gap">
            <StatusBadge status={m.latest_run.verdict ?? m.latest_run.status} />
            <Link to={`/runs/${m.latest_run.id}`}><code>{m.latest_run.id}</code></Link>
            <span>{m.latest_run.impl_id}</span>
            <span className="muted">×</span>
            <span>{m.latest_run.scenario_id}</span>
            <span className="muted">seed {m.latest_run.seed} · {fmtTime(m.latest_run.created_at)}</span>
          </div>
        ) : (
          <p className="muted">No runs recorded yet.</p>
        )}
      </Panel>
    </div>
  );
}

// For references a FAIL is a defect (red); for mutants a FAIL is a detection
// (accent) and survival is neutral, never "good".
function cellClass(c: MatrixCell | undefined, mutant: boolean) {
  if (!c) return "cell-empty";
  if (c.infra_error) return "cell-infra";
  if (mutant) return c.fail ? "cell-detect" : c.inconclusive ? "cell-inc" : "cell-survive";
  if (c.fail) return "cell-fail";
  if (c.inconclusive) return "cell-inc";
  return "cell-pass";
}

function cellLabel(c: MatrixCell | undefined, mutant: boolean) {
  if (!c) return "–";
  if (c.infra_error) return `! ${c.infra_error} infra`;
  if (mutant) return c.fail ? `● ${c.fail}/${c.runs} detected` : c.inconclusive ? `? ${c.inconclusive}/${c.runs}` : `○ 0/${c.runs} survived`;
  return c.fail ? `✕ ${c.fail}/${c.runs} fail` : c.inconclusive ? `? ${c.inconclusive}/${c.runs}` : `✓ 0/${c.runs} fail`;
}

function Matrix({ bench }: { bench: Benchmark }) {
  const s = bench.summary!;
  const scen = Array.from(new Set(Object.values(s.matrix).flatMap((r) => Object.keys(r)))).sort();
  const [tip, setTip] = useState<string | null>(null);
  const mutants = new Set(s.mutants.map((m) => m.impl_id));
  return (
    <div className="matrix-block">
      <h3>Detection matrix</h3>
      <p className="muted small">
        Each cell shows runs with verdict FAIL out of runs executed for that implementation and scenario. Reference
        rows: ✓ no failures, ✕ failures (a defect). Mutant rows: ● detected (at least one failing run), ○ survived
        (no run failed). Hover over a cell to see which invariants failed.
      </p>
      <div className="table-wrap">
        <table className="table matrix">
          <thead>
            <tr>
              <th>Implementation</th>
              {scen.map((x) => <th key={x} title={x}>{shortScenario(x)}</th>)}
            </tr>
          </thead>
          <tbody>
            {Object.entries(s.matrix).map(([impl, cells]) => (
              <tr key={impl}>
                <th scope="row" className="mono">{impl}</th>
                {scen.map((sc) => {
                  const c = cells[sc];
                  const inv = c ? Object.entries(c.failed_invariants).map(([k, v]) => `${k} (${v})`).join(", ") : "";
                  const isMutant = mutants.has(impl);
                  const label = cellLabel(c, isMutant);
                  return (
                    <td key={sc} className={`cell ${cellClass(c, isMutant)}`}
                      onMouseEnter={() => setTip(`${impl} × ${sc}: ${c ? `${c.fail} fail, ${c.pass} pass, ${c.inconclusive} inconclusive, ${c.infra_error} infra` : "not run"}${inv ? ` — failed: ${inv}` : ""}`)}
                      onMouseLeave={() => setTip(null)} title={inv || undefined}>
                      {label}
                    </td>
                  );
                })}
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      <div className="tipline" aria-live="polite">{tip ?? " "}</div>
    </div>
  );
}

function MutantTable({ bench }: { bench: Benchmark }) {
  const s = bench.summary!;
  return (
    <div className="matrix-block">
      <h3>Mutant validation</h3>
      <div className="table-wrap">
        <table className="table">
          <thead>
            <tr>
              <th>Mutant</th><th>Intended invariant</th><th>Intended-scenario detection</th><th>Killed as intended</th>
              <th>Other invariants failed</th><th>Conventional suite</th>
            </tr>
          </thead>
          <tbody>
            {s.mutants.map((m) => (
              <tr key={m.impl_id}>
                <td className="mono">{m.impl_id}</td>
                <td className="mono">{m.intended_invariant}</td>
                <td>{m.intended_detection} runs</td>
                <td>{m.killed_as_intended ? <span className="badge st-detect">● Killed</span> : <span className="badge st-na">○ Not killed</span>}</td>
                <td className="mono small">{m.other_invariants_failed.join(", ") || "–"}</td>
                <td>
                  {(() => {
                    // pass / fail / infra_error stay distinct; per-test statuses are authoritative.
                    const tests = s.conventional[m.impl_id]?.tests;
                    if (!tests) return <span className="muted">not run</span>;
                    const failed = tests.filter((t) => t.status === "fail").map((t) => t.id);
                    const infra = tests.filter((t) => t.status === "infra_error").map((t) => t.id);
                    const passed = tests.filter((t) => t.status === "pass").length;
                    return (
                      <span>
                        {passed}/{tests.length} pass
                        {failed.length > 0 && <span className="muted"> (fails {failed.join(", ")})</span>}
                        {infra.length > 0 && <span className="badge st-infra"> ! infrastructure error: {infra.join(", ")} (not evaluated)</span>}
                        {m.detected && !failed.length && !infra.length && <span className="tag">passes conventional, fails adversarial</span>}
                        {m.detected && !failed.length && infra.length > 0 && <span className="tag">conventional inconclusive, fails adversarial</span>}
                      </span>
                    );
                  })()}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  );
}
