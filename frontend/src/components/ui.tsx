import { ReactNode, useEffect, useState } from "react";
import { ApiError, Status, Verdict } from "../api";

const STATUS_META: Record<string, { label: string; icon: string; cls: string }> = {
  pass: { label: "Pass", icon: "✓", cls: "st-pass" },
  fail: { label: "Fail", icon: "✕", cls: "st-fail" },
  inconclusive: { label: "Inconclusive", icon: "?", cls: "st-inc" },
  infra_error: { label: "Infra error", icon: "!", cls: "st-infra" },
  not_applicable: { label: "n/a", icon: "–", cls: "st-na" },
  queued: { label: "Queued", icon: "…", cls: "st-na" },
  running: { label: "Running", icon: "◌", cls: "st-run" },
};

/** Status is always conveyed by icon + text, never by colour alone. */
export function StatusBadge({ status, compact }: { status: Status | Verdict | string | null; compact?: boolean }) {
  const m = STATUS_META[status ?? ""] ?? { label: String(status ?? "unknown"), icon: "·", cls: "st-na" };
  return (
    <span className={`badge ${m.cls}`} title={m.label}>
      <span aria-hidden="true">{m.icon}</span>
      {!compact && <span>{m.label}</span>}
      {compact && <span className="sr-only">{m.label}</span>}
    </span>
  );
}

export function useAsync<T>(fn: () => Promise<T>, deps: unknown[] = []) {
  const [data, setData] = useState<T | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const [tick, setTick] = useState(0);
  useEffect(() => {
    let alive = true;
    setLoading(true);
    fn()
      .then((d) => alive && (setData(d), setError(null)))
      .catch((e: ApiError | Error) => alive && setError(e.message))
      .finally(() => alive && setLoading(false));
    return () => {
      alive = false;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [...deps, tick]);
  return { data, error, loading, reload: () => setTick((t) => t + 1) };
}

export function Loading({ what = "Loading" }: { what?: string }) {
  return <div className="muted pad">{what}…</div>;
}

export function ErrorBox({ message }: { message: string }) {
  return (
    <div className="error-box" role="alert">
      <strong>Error.</strong> {message}
    </div>
  );
}

export function Panel({ title, actions, children, sub }: { title?: ReactNode; actions?: ReactNode; children: ReactNode; sub?: ReactNode }) {
  return (
    <section className="panel">
      {(title || actions) && (
        <header className="panel-head">
          <div>
            {title && <h2>{title}</h2>}
            {sub && <div className="panel-sub">{sub}</div>}
          </div>
          {actions && <div className="panel-actions">{actions}</div>}
        </header>
      )}
      <div className="panel-body">{children}</div>
    </section>
  );
}

/** Stat tile. `value === null` renders as explicitly unavailable. */
export function Stat({ label, value, note }: { label: string; value: ReactNode | null; note?: ReactNode }) {
  return (
    <div className="stat">
      <div className="stat-label">{label}</div>
      <div className={`stat-value ${value === null ? "unavailable" : ""}`}>{value === null ? "Unavailable" : value}</div>
      {note && <div className="stat-note">{note}</div>}
    </div>
  );
}

export function Hash({ value }: { value: string | null | undefined }) {
  if (!value) return <span className="muted">–</span>;
  return (
    <code className="hash" title={value}>
      {value.slice(0, 12)}
    </code>
  );
}

export function Json({ value }: { value: unknown }) {
  return <pre className="json">{JSON.stringify(value, null, 2)}</pre>;
}

export function fmtTime(iso: string | null | undefined) {
  if (!iso) return "–";
  const d = new Date(iso);
  return d.toLocaleString(undefined, { year: "numeric", month: "short", day: "2-digit", hour: "2-digit", minute: "2-digit" });
}

export function shortScenario(id: string) {
  return id.split("_")[0];
}

export function Link({ to, children, className }: { to: string; children: ReactNode; className?: string }) {
  return (
    <a href={`#${to}`} className={className}>
      {children}
    </a>
  );
}
