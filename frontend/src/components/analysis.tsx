import { useEffect, useState } from "react";
import { AnalysisKind, AnalysisSummary, api } from "../api";
import { ErrorBox, Link, Loading, fmtTime } from "./ui";

/** Fetch an analysis and keep polling while the backend reports it queued or running. */
export function usePolled<T extends { status: string }>(fetcher: () => Promise<T>, key: string) {
  const [data, setData] = useState<T | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [tick, setTick] = useState(0);
  useEffect(() => {
    let alive = true;
    let timer: number | undefined;
    setError(null);
    setData(null);  // never show the previous analysis while a different one loads
    const load = () =>
      fetcher()
        .then((d) => {
          if (!alive) return;
          setData(d);
          if (d.status === "queued" || d.status === "running") timer = window.setTimeout(load, 2000);
        })
        .catch((e) => alive && setError((e as Error).message));
    load();
    return () => {
      alive = false;
      window.clearTimeout(timer);
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [key, tick]);
  return { data, error, reload: () => setTick((t) => t + 1) };
}

const STATUS: Record<string, { icon: string; cls: string; label: string }> = {
  queued: { icon: "…", cls: "st-na", label: "Queued" },
  running: { icon: "◌", cls: "st-run", label: "Running" },
  completed: { icon: "✓", cls: "st-pass", label: "Completed" },
  stopped: { icon: "■", cls: "st-inc", label: "Stopped at a limit (incomplete)" },
  incomplete: { icon: "■", cls: "st-inc", label: "Finished with gaps (incomplete)" },
  budget_exhausted: { icon: "■", cls: "st-inc", label: "Budget exhausted (incomplete)" },
  interrupted: { icon: "⏸", cls: "st-inc", label: "Interrupted (incomplete)" },
  error: { icon: "!", cls: "st-infra", label: "Error" },
  flaky: { icon: "≈", cls: "st-inc", label: "Flaky: not reproducible reliably" },
  not_reproducible: { icon: "?", cls: "st-inc", label: "Original failure not reproduced" },
  rejected: { icon: "!", cls: "st-infra", label: "Rejected" },
  blocked: { icon: "!", cls: "st-infra", label: "Blocked (nothing executed)" },
  defined: { icon: "○", cls: "st-na", label: "Defined (no report yet)" },
};

/** Analysis status: icon + text, so incomplete states are never mistaken for completion. */
export function AnalysisStatus({ status }: { status: string }) {
  const s = STATUS[status] ?? (status.startsWith("reported")
    ? { icon: status.includes("(complete)") ? "✓" : "■", cls: status.includes("(complete)") ? "st-pass" : "st-inc", label: status }
    : { icon: "·", cls: "st-na", label: status });
  return <span className={`badge ${s.cls}`}><span aria-hidden="true">{s.icon}</span><span>{s.label}</span></span>;
}

export function downloadJson(name: string, value: unknown) {
  const a = document.createElement("a");
  a.href = `data:application/json;charset=utf-8,${encodeURIComponent(JSON.stringify(value, null, 1))}`;
  a.download = name;
  a.click();
}

export function AnalysisList({ kind, base, refreshKey, describe }: {
  kind: AnalysisKind; base: string; refreshKey?: unknown; describe: (a: AnalysisSummary) => string;
}) {
  const [items, setItems] = useState<AnalysisSummary[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  useEffect(() => {
    let alive = true;
    let timer: number | undefined;
    const load = () => api.analyses(kind).then((r) => {
      if (!alive) return;
      setItems(r);
      if (r.some((x) => x.status === "queued" || x.status === "running")) timer = window.setTimeout(load, 3000);
    }).catch((e) => alive && setError((e as Error).message));
    load();
    return () => { alive = false; window.clearTimeout(timer); };
  }, [kind, refreshKey]);
  if (error) return <ErrorBox message={error} />;
  if (!items) return <Loading />;
  if (!items.length) return <p className="muted">None yet.</p>;
  return (
    <div className="table-wrap">
      <table className="table">
        <thead><tr><th>Id</th><th>Status</th><th>Configuration</th><th>Created</th></tr></thead>
        <tbody>{items.map((a) => (
          <tr key={a.id}>
            <td><Link to={`${base}/${a.id}`}><code>{a.id}</code></Link></td>
            <td><AnalysisStatus status={a.status} /></td>
            <td className="mono small">{describe(a)}</td>
            <td className="small">{fmtTime(a.created_at)}</td>
          </tr>))}
        </tbody>
      </table>
    </div>
  );
}
