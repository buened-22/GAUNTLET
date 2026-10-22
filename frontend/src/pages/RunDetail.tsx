import { useEffect, useMemo, useState } from "react";
import { api, RunDetail as Run, Trace, TraceEvent } from "../api";
import { Markdown } from "../components/Markdown";
import { Legend, Timeline } from "../components/Timeline";
import { ErrorBox, Hash, Json, Link, Loading, Panel, Stat, StatusBadge, fmtTime } from "../components/ui";
import { ReplayStatus } from "./Differential";

type Tab = "timeline" | "invariants" | "ledger" | "snapshots" | "report";

export function RunDetail({ id, tab, setTab }: { id: string; tab: Tab; setTab: (t: Tab) => void }) {
  const [run, setRun] = useState<Run | null>(null);
  const [trace, setTrace] = useState<Trace | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [selected, setSelected] = useState<number | null>(null);
  const [replays, setReplays] = useState<string[]>([]);

  // Poll until the backend reports completion; never show results before that.
  useEffect(() => {
    let alive = true;
    let timer: number | undefined;
    setRun(null);
    setTrace(null);
    const load = async () => {
      try {
        const r = await api.run(id);
        if (!alive) return;
        setRun(r);
        if (r.status === "completed" && r.invariants) {
          const t = await api.trace(id);
          if (alive) setTrace(t);
        } else if (r.status !== "completed") {
          timer = window.setTimeout(load, 1500);
        }
      } catch (e) {
        if (alive) setError((e as Error).message);
      }
    };
    load();
    return () => {
      alive = false;
      window.clearTimeout(timer);
    };
  }, [id]);

  if (error) return <ErrorBox message={error} />;
  if (!run) return <Loading />;
  if (run.status !== "completed" || !run.invariants) {
    return (
      <Panel title={`Run ${id}`}>
        <div className="row gap"><StatusBadge status={run.status === "completed" ? run.verdict : run.status} />
          <span>{run.impl_id} × {run.scenario_id} · seed {run.seed}</span></div>
        {run.infra_error ? <ErrorBox message={run.infra_error} /> :
          <p className="muted">The backend reports this run as <strong>{run.status}</strong>. Results appear only after it completes.</p>}
      </Panel>
    );
  }

  const triggers = run.invariants.filter((i) => i.status === "fail" && i.trigger_seq).map((i) => ({ seq: i.trigger_seq!, label: i.id }));
  const jump = (seq: number) => { setSelected(seq); setTab("timeline"); };

  return (
    <div className="stack">
      <Panel title={<span className="row gap">Run <code>{run.id}</code> <StatusBadge status={run.verdict} /></span>}
        sub={`${run.impl_id} (${run.impl_kind}, strategy ${run.strategy}) × ${run.scenario_id} · seed ${run.seed} · ${fmtTime(run.created_at)}`
          + (run.origin ? ` · created by ${run.origin.kind} ${run.origin.id ?? run.origin.of ?? ""}` : "")
          + (run.schedule_source && run.schedule_source !== "catalog" ? ` · ${run.schedule_source} schedule` : "")}
        actions={<span className="row gap">
          <button className="btn" onClick={() => api.replayRun(run.id).then((x) => setReplays((p) => [x.id, ...p]))}>Replay</button>
          {run.verdict === "fail" && <Link to={`/minimize?run=${run.id}`} className="btn">Minimize failure</Link>}
        </span>}>
        <div className="stat-row">
          <Stat label="Granted statements" value={run.metrics.statements} />
          <Stat label="Attempts" value={run.metrics.attempts} note={`${run.metrics.acknowledged} ok · ${run.metrics.failed} failed · ${run.metrics.uncertain} uncertain`} />
          <Stat label="Fault-tagged events" value={run.metrics.faults_injected} note={`${run.metrics.crashes} process crashes · ${run.metrics.recoveries} recovery actions`} />
          <Stat label="Lock waits" value={run.metrics.lock_waits} />
          <Stat label="Invariants" value={`${run.metrics.invariants.fail ?? 0} fail`} note={`${run.metrics.invariants.pass ?? 0} pass · ${run.metrics.invariants.inconclusive ?? 0} inconclusive · ${run.metrics.invariants.not_applicable ?? 0} n/a`} />
          <Stat label="Wall time" value={`${(run.duration_ms / 1000).toFixed(1)} s`} note="observational only" />
        </div>
        <div className="hash-row small">
          <span>schedule <Hash value={run.schedule_hash} /></span>
          <span>trace <Hash value={run.trace_hash} /></span>
          <span>outcome <Hash value={run.outcome_hash} /></span>
          <span className="muted">PostgreSQL {run.environment.postgres ?? "?"} · Python {run.environment.python}</span>
        </div>
        {replays.map((x) => <ReplayStatus key={x} id={x} />)}
        {run.infra_error && <ErrorBox message={run.infra_error} />}
        {run.inconclusive.length > 0 && <div className="warn-box">Inconclusive conditions: {run.inconclusive.join("; ")}</div>}
      </Panel>

      <div className="tabs" role="tablist">
        {(["timeline", "invariants", "ledger", "snapshots", "report"] as Tab[]).map((t) => (
          <button key={t} role="tab" aria-selected={tab === t} className={tab === t ? "active" : ""} onClick={() => setTab(t)}>
            {t === "timeline" ? "Execution timeline" : t === "invariants" ? "Invariant report" : t === "ledger" ? "Client ledger" :
              t === "snapshots" ? "Oracle snapshots" : "Research report"}
          </button>
        ))}
      </div>

      {tab === "timeline" && (trace ? <TimelineTab trace={trace} selected={selected} setSelected={setSelected} triggers={triggers} /> : <Loading what="Loading trace" />)}
      {tab === "invariants" && <InvariantsTab run={run} onJump={jump} />}
      {tab === "ledger" && <LedgerTab run={run} onJump={jump} />}
      {tab === "snapshots" && <SnapshotsTab run={run} />}
      {tab === "report" && <ReportTab id={run.id} />}
    </div>
  );
}

const KIND_FILTERS = ["all", "operations", "statements", "faults & recovery", "oracle"] as const;

function TimelineTab({ trace, selected, setSelected, triggers }: { trace: Trace; selected: number | null; setSelected: (n: number) => void; triggers: { seq: number; label: string }[] }) {
  const [kind, setKind] = useState<(typeof KIND_FILTERS)[number]>("all");
  const [actor, setActor] = useState("all");
  const actors = useMemo(() => Array.from(new Set(trace.events.map((e) => e.actor).filter(Boolean))) as string[], [trace]);
  const ev = trace.events.find((e) => e.seq === selected) ?? null;
  const rows = trace.events.filter((e) => {
    if (actor !== "all" && e.actor !== actor) return false;
    if (kind === "operations") return ["invoke", "respond"].includes(e.kind);
    if (kind === "statements") return ["stmt", "unblocked", "blocked"].includes(e.kind);
    if (kind === "faults & recovery") return Boolean(e.fault) || ["crash", "restart", "paused", "blocked"].includes(e.kind) || Boolean(e.retry) || Boolean(e.recovery);
    if (kind === "oracle") return ["snapshot", "stage"].includes(e.kind);
    return true;
  });
  const attempt = ev?.attempt_id ? trace.attempts.find((a) => a.attempt_id === ev.attempt_id) : null;
  return (
    <div className="stack">
      <Panel title="Execution timeline" sub={`${trace.events.length} events in logical order (x = sequence number). Click a mark to inspect it.`}>
        <Legend />
        <Timeline events={trace.events} selected={selected} onSelect={setSelected} triggers={triggers} />
      </Panel>
      <div className="split">
        <Panel title="Events" actions={
          <div className="row gap">
            <select value={kind} onChange={(e) => setKind(e.target.value as typeof kind)} aria-label="Event kind filter">
              {KIND_FILTERS.map((k) => <option key={k}>{k}</option>)}
            </select>
            <select value={actor} onChange={(e) => setActor(e.target.value)} aria-label="Actor filter">
              <option value="all">all actors</option>
              {actors.map((a) => <option key={a}>{a}</option>)}
            </select>
          </div>}>
          <div className="table-wrap tall">
            <table className="table events">
              <thead><tr><th>seq</th><th>kind</th><th>actor</th><th>detail</th></tr></thead>
              <tbody>
                {rows.map((e) => (
                  <tr key={e.seq} className={e.seq === selected ? "selected" : ""} onClick={() => setSelected(e.seq)}>
                    <td className="mono">{e.seq}</td>
                    <td><EventKind e={e} /></td>
                    <td className="mono">{e.actor ?? "harness"}</td>
                    <td className="mono small ellipsis">{eventDetail(e)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </Panel>
        <Panel title={ev ? `Event #${ev.seq}` : "Event detail"}>
          {!ev ? <p className="muted">Select an event in the timeline or table.</p> : (
            <>
              <Json value={ev} />
              {attempt && (<><h4>Ledger entry for {attempt.attempt_id}</h4><Json value={attempt} /></>)}
            </>
          )}
        </Panel>
      </div>
    </div>
  );
}

function EventKind({ e }: { e: TraceEvent }) {
  const cls = e.kind === "crash" ? "k-crit" : e.kind === "blocked" ? "k-serious" : (e.kind === "respond" && !e.delivered) ? "k-warn" : "";
  return <span className={`kind ${cls}`}>{e.kind}{e.fault ? ` [${e.fault}]` : ""}</span>;
}

function eventDetail(e: TraceEvent): string {
  if (e.kind === "stmt" || e.kind === "unblocked" || e.kind === "blocked")
    return `${e.cls}: ${e.sql}${e.error ? `  → ERROR ${(e.error as { sqlstate?: string }).sqlstate}` : ""}`;
  if (e.kind === "invoke") return `${e.attempt_id} ${e.op} ${JSON.stringify(e.args)}${e.retry ? " (retry)" : ""}`;
  if (e.kind === "respond") {
    const o = e.outcome as { status: string; result?: unknown; error?: { code?: string; sqlstate?: string } };
    return `${e.attempt_id} ${e.delivered ? "" : "LOST "}${o.status} ${o.status === "ok" ? JSON.stringify(o.result) : `${o.error?.code ?? ""} ${o.error?.sqlstate ?? ""}`}`;
  }
  if (e.kind === "stage") return `${e.label} ${e.note ?? ""}`;
  if (e.kind === "snapshot") return `snapshot '${e.label}'`;
  if (e.kind === "crash") return `process killed; in-flight ${e.attempt_id ?? "none"}`;
  if (e.kind === "paused") return `paused ${e.cond}; next: ${e.next_sql ?? "-"}`;
  return Object.entries(e).filter(([k]) => !["seq", "kind", "actor", "t_ms", "directive"].includes(k)).map(([k, v]) => `${k}=${typeof v === "object" ? JSON.stringify(v) : v}`).join(" ").slice(0, 200);
}

function InvariantsTab({ run, onJump }: { run: Run; onJump: (seq: number) => void }) {
  const order = { fail: 0, infra_error: 1, inconclusive: 2, pass: 3, not_applicable: 4 } as Record<string, number>;
  const invs = [...run.invariants].sort((a, b) => order[a.status] - order[b.status]);
  return (
    <Panel title="Invariant report" sub="Results come from the independent oracle. Diagnoses based on model relaxations mean the evidence is consistent with the cause; they do not prove it.">
      <div className="inv-list">
        {invs.map((i) => (
          <details key={i.id} className={`inv inv-${i.status}`} open={i.status === "fail"}>
            <summary>
              <StatusBadge status={i.status} />
              <code>{i.id}</code>
              <span className="inv-title">{i.title}</span>
              <span className="muted small inv-cat">{i.category}</span>
            </summary>
            <p>{i.summary}</p>
            {i.trigger_seq && <button className="btn small" onClick={() => onJump(i.trigger_seq!)}>Show triggering event #{i.trigger_seq}</button>}
            {i.evidence.length > 0 && <Evidence items={i.evidence} onJump={onJump} />}
          </details>
        ))}
      </div>
    </Panel>
  );
}

function LedgerTab({ run, onJump }: { run: Run; onJump: (seq: number) => void }) {
  return (
    <Panel title="Client ledger" sub="Append-only record kept by the harness acting as client: what each client asked for, and what it was told.">
      <div className="table-wrap tall">
        <table className="table">
          <thead><tr><th>Attempt</th><th>Actor</th><th>Op</th><th>Args</th><th>Status</th><th>Reason</th><th>Result / error</th><th>Window (seq)</th></tr></thead>
          <tbody>
            {run.attempts.map((a) => (
              <tr key={a.attempt_id}>
                <td className="mono">{a.attempt_id}</td>
                <td className="mono">{a.actor}</td>
                <td className="mono">{a.op}</td>
                <td className="mono small">{JSON.stringify(a.args)}</td>
                <td><span className={`ledger-${a.status}`}>{a.status}</span></td>
                <td className="small">{a.reason}</td>
                <td className="mono small">{a.status === "ok" ? JSON.stringify(a.result) : a.error ? `${a.error.code ?? ""} ${a.error.sqlstate ?? ""} ${a.error.message ?? ""}` : "–"}</td>
                <td className="mono small">
                  <button className="linkish" onClick={() => onJump(a.invoke_seq)}>{a.invoke_seq}</button>–
                  {a.complete_seq ? <button className="linkish" onClick={() => onJump(a.complete_seq!)}>{a.complete_seq}</button> : "∞"}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </Panel>
  );
}

function SnapshotsTab({ run }: { run: Run }) {
  const [idx, setIdx] = useState(run.snapshots.length - 1);
  const s = run.snapshots[idx];
  const initial = run.initial_state;
  return (
    <Panel title="Oracle snapshots" sub="Raw database state read by the harness's own connection. Implementation code is not involved."
      actions={<select value={idx} onChange={(e) => setIdx(Number(e.target.value))} aria-label="Snapshot">
        {run.snapshots.map((x, i) => <option key={i} value={i}>{i}: {x.label} (seq {x.seq})</option>)}
      </select>}>
      {s && (
        <>
          <p className="small muted">Columns: {s.columns.join(", ")} · NOT NULL: {Object.entries(s.not_null).filter(([, v]) => v).map(([k]) => k).join(", ")} ·
            triggers: {s.triggers.join(", ") || "none"} · implementation-reported stage: {s.impl_reported_stage ?? "–"} (informational, not trusted)</p>
          <div className="table-wrap tall">
            <table className="table">
              <thead><tr><th>id</th>{s.columns.includes("total") && <th>total</th>}{s.columns.includes("total_cents") && <th>total_cents</th>}<th>agree</th><th>initial</th></tr></thead>
              <tbody>
                {s.rows.map((r) => {
                  const hasBoth = s.columns.includes("total") && s.columns.includes("total_cents");
                  const agree = !hasBoth || r.total_cents == null ? null : Math.round(Number(r.total) * 100) === r.total_cents;
                  return (
                    <tr key={r.id}>
                      <td className="mono">{r.id}</td>
                      {s.columns.includes("total") && <td className="mono">{r.total ?? "NULL"}</td>}
                      {s.columns.includes("total_cents") && <td className="mono">{r.total_cents ?? "NULL"}</td>}
                      <td>{agree === null ? <span className="muted">–</span> : agree ? "yes" : <strong className="txt-crit">NO</strong>}</td>
                      <td className="mono muted">{initial[String(r.id)] ?? "–"}</td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>
        </>
      )}
    </Panel>
  );
}

function ReportTab({ id }: { id: string }) {
  const [md, setMd] = useState<string | null>(null);
  const [err, setErr] = useState<string | null>(null);
  useEffect(() => { api.report(id).then(setMd).catch((e) => setErr(e.message)); }, [id]);
  if (err) return <ErrorBox message={err} />;
  if (!md) return <Loading what="Generating report" />;
  return (
    <Panel title="Research report" sub="Generated by the backend from this run's recorded data."
      actions={<a className="btn" href={`data:text/markdown;charset=utf-8,${encodeURIComponent(md)}`} download={`gauntlet-run-${id}.md`}>Download .md</a>}>
      <Markdown source={md} />
    </Panel>
  );
}

export function RunLink({ id }: { id: string }) {
  return <Link to={`/runs/${id}`}><code>{id}</code></Link>;
}

interface OrderEvidence {
  order_id: number;
  initial: number | string;
  explained_by: string[];
  first_conflict: { observation: string; seq: number | null; observed: string[] } | null;
  writes: { logical_id: string; kind: string; arg_cents: number; attempts: { attempt_id: string; status: string; out_cents: number | null }[] }[];
}

const cents = (v: number | string | null | undefined) =>
  typeof v === "number" ? `${v < 0 ? "-" : ""}${Math.floor(Math.abs(v) / 100)}.${String(Math.abs(v) % 100).padStart(2, "0")}` : String(v ?? "–");

/** Readable rendering of per-order linearizability evidence, with the raw JSON available. */
function Evidence({ items, onJump }: { items: unknown[]; onJump: (seq: number) => void }) {
  const orders = items.filter((x): x is OrderEvidence => typeof x === "object" && x !== null && "order_id" in x && "writes" in x);
  return (
    <>
      {orders.length > 0 && (
        <div className="table-wrap">
          <table className="table compact">
            <thead><tr><th>Order</th><th>Initial</th><th>Client writes (from ledger)</th><th>Conflicting observation</th><th>Consistent with</th></tr></thead>
            <tbody>
              {orders.map((o) => (
                <tr key={o.order_id}>
                  <td className="mono">{o.order_id}</td>
                  <td className="mono">{o.initial === "ABSENT" ? "absent" : cents(o.initial)}</td>
                  <td className="mono small">{o.writes.map((w) => (
                    <div key={w.logical_id}>{w.logical_id} {w.kind} {cents(w.arg_cents)} → {w.attempts.map((a) => `${a.attempt_id}:${a.status}${a.out_cents != null ? `=${cents(a.out_cents)}` : ""}`).join(", ")}</div>
                  ))}</td>
                  <td className="small">{o.first_conflict ? (
                    <>{o.first_conflict.observation} observed <span className="mono">{o.first_conflict.observed.map((v) => (/^-?\d+$/.test(v) ? cents(Number(v)) : v)).join(" / ")}</span>
                      {o.first_conflict.seq != null && <> at <button className="linkish" onClick={() => onJump(o.first_conflict!.seq!)}>#{o.first_conflict.seq}</button></>}</>
                  ) : "–"}</td>
                  <td className="mono small">{o.explained_by.join(", ") || "no relaxation (unexplained)"}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
      <details><summary className="small muted">Raw evidence ({items.length} item{items.length === 1 ? "" : "s"})</summary><Json value={items} /></details>
    </>
  );
}
