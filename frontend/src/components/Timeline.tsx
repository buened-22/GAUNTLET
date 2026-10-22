import { useMemo, useRef, useState } from "react";
import { TraceEvent } from "../api";

// Swimlane view of the ordered event trace. X = position in the logical
// order (seq), one lane per actor plus a harness lane. Marks are thin; every
// mark has an enlarged invisible hit target and a hover tooltip; clicking
// selects the event for inspection.

const LANE_H = 30;
const PX = 7;
const LEFT = 92;
const TOP = 26;

const STMT_CLASS: Record<string, string> = {
  read: "var(--series-1)",
  write: "var(--series-2)",
  commit: "var(--series-3)",
  ddl: "var(--series-4)",
};

export const TIMELINE_LEGEND = [
  { label: "read stmt", color: STMT_CLASS.read, shape: "tick" },
  { label: "write stmt", color: STMT_CLASS.write, shape: "tick" },
  { label: "commit", color: STMT_CLASS.commit, shape: "tick" },
  { label: "DDL", color: STMT_CLASS.ddl, shape: "tick" },
  { label: "begin / rollback", color: "var(--text-muted)", shape: "tick" },
  { label: "invoke → response", color: "var(--text-primary)", shape: "dot" },
  { label: "response lost", color: "var(--status-warning)", shape: "ring" },
  { label: "lock wait", color: "var(--status-serious)", shape: "square" },
  { label: "process crash", color: "var(--status-critical)", shape: "x" },
  { label: "oracle snapshot", color: "var(--text-secondary)", shape: "diamond" },
];

function describe(e: TraceEvent): string {
  const parts = [`#${e.seq} ${e.kind}`];
  if (e.actor) parts.push(`actor ${e.actor}`);
  if (e.attempt_id) parts.push(String(e.attempt_id));
  if (e.op) parts.push(String(e.op));
  if (e.sql) parts.push(String(e.sql).slice(0, 140));
  if (e.label) parts.push(String(e.label));
  if (e.fault) parts.push(`fault ${e.fault}`);
  if (e.kind === "respond") {
    const o = e.outcome as { status?: string } | undefined;
    parts.push(e.delivered ? `delivered (${o?.status})` : "response LOST");
  }
  return parts.join(" · ");
}

export function Timeline({ events, selected, onSelect, triggers = [] }: {
  events: TraceEvent[];
  selected: number | null;
  onSelect: (seq: number) => void;
  triggers?: { seq: number; label: string }[];
}) {
  const lanes = useMemo(() => {
    const order: string[] = ["harness"];
    for (const e of events) if (e.actor && !order.includes(e.actor)) order.push(e.actor);
    return order;
  }, [events]);
  const [hover, setHover] = useState<{ x: number; y: number; text: string } | null>(null);
  const wrap = useRef<HTMLDivElement>(null);

  const width = LEFT + events.length * PX + 40;
  const height = TOP + lanes.length * LANE_H + 8;
  const laneY = (actor: string | null) => TOP + lanes.indexOf(actor ?? "harness") * LANE_H + LANE_H / 2;
  const xOf = (i: number) => LEFT + i * PX + PX / 2;
  const indexOfSeq = new Map(events.map((e, i) => [e.seq, i]));

  const mark = (e: TraceEvent, i: number) => {
    const x = xOf(i);
    const y = laneY(e.actor);
    switch (e.kind) {
      case "stmt":
      case "unblocked": {
        const cls = String(e.cls ?? "");
        const err = Boolean(e.error);
        return <line x1={x} x2={x} y1={y - 8} y2={y + 8} stroke={STMT_CLASS[cls] ?? "var(--text-muted)"}
          strokeWidth={2} strokeLinecap="round" strokeDasharray={err ? "2 2" : undefined} />;
      }
      case "invoke":
      case "protocol_invoke":
        return <circle cx={x} cy={y} r={3} fill="var(--surface-1)" stroke="var(--text-primary)" strokeWidth={1.5} />;
      case "respond":
      case "protocol_respond": {
        if (e.kind === "respond" && !e.delivered)
          return <circle cx={x} cy={y} r={4.5} fill="none" stroke="var(--status-warning)" strokeWidth={2} />;
        const failed = (e.outcome as { status?: string } | undefined)?.status !== "ok";
        return <circle cx={x} cy={y} r={4} fill={failed ? "var(--status-critical)" : "var(--text-primary)"} />;
      }
      case "blocked":
        return <rect x={x - 4} y={y - 4} width={8} height={8} fill="none" stroke="var(--status-serious)" strokeWidth={2} />;
      case "crash":
        return (
          <g stroke="var(--status-critical)" strokeWidth={2.5} strokeLinecap="round">
            <line x1={x - 5} y1={y - 5} x2={x + 5} y2={y + 5} />
            <line x1={x - 5} y1={y + 5} x2={x + 5} y2={y - 5} />
          </g>
        );
      case "snapshot":
        return <path d={`M${x} ${y - 6} L${x + 5} ${y} L${x} ${y + 6} L${x - 5} ${y} Z`} fill="var(--text-secondary)" />;
      case "spawn":
      case "restart":
        return <path d={`M${x - 4} ${y - 5} L${x + 5} ${y} L${x - 4} ${y + 5} Z`} fill="var(--series-3)" />;
      case "drain":
        return <path d={`M${x + 4} ${y - 5} L${x - 5} ${y} L${x + 4} ${y + 5} Z`} fill="var(--text-muted)" />;
      case "paused":
        return <line x1={x - 3} x2={x + 3} y1={y} y2={y} stroke="var(--status-warning)" strokeWidth={3} />;
      case "stage":
        return null;
      default:
        return <circle cx={x} cy={y} r={2} fill="var(--text-muted)" />;
    }
  };

  return (
    <div className="timeline-wrap" ref={wrap} onMouseLeave={() => setHover(null)}>
      <svg width={width} height={height} role="img" aria-label={`Event trace with ${events.length} events`}>
        {lanes.map((l, li) => (
          <g key={l}>
            <line x1={LEFT} x2={width - 20} y1={TOP + li * LANE_H + LANE_H / 2} y2={TOP + li * LANE_H + LANE_H / 2}
              stroke="var(--grid)" strokeWidth={1} />
            <text x={8} y={TOP + li * LANE_H + LANE_H / 2 + 4} className="lane-label">{l}</text>
          </g>
        ))}
        {events.map((e, i) =>
          e.kind === "stage" ? (
            <g key={`st${e.seq}`}>
              <line x1={xOf(i)} x2={xOf(i)} y1={TOP - 4} y2={height - 4} stroke="var(--text-muted)" strokeDasharray="3 3" />
              <text x={xOf(i) + 3} y={14} className="stage-label">{String(e.label)}</text>
            </g>
          ) : null)}
        {triggers.map((t) => {
          const i = indexOfSeq.get(t.seq);
          if (i === undefined) return null;
          return (
            <g key={`tr${t.seq}${t.label}`}>
              <line x1={xOf(i)} x2={xOf(i)} y1={TOP - 4} y2={height - 4} stroke="var(--status-critical)" strokeWidth={2} />
              <title>{`Violation trigger: ${t.label} (seq ${t.seq})`}</title>
            </g>
          );
        })}
        {selected !== null && indexOfSeq.has(selected) && (
          <rect x={xOf(indexOfSeq.get(selected)!) - PX / 2} y={TOP - 4} width={PX} height={height - TOP}
            fill="var(--selection)" />
        )}
        {events.map((e, i) => <g key={e.seq}>{mark(e, i)}</g>)}
        {events.map((e, i) => (
          <rect key={`h${e.seq}`} x={xOf(i) - PX / 2} y={laneY(e.actor) - LANE_H / 2} width={PX} height={LANE_H}
            fill="transparent" style={{ cursor: "pointer" }}
            onClick={() => onSelect(e.seq)}
            onMouseMove={(ev) => {
              const r = wrap.current!.getBoundingClientRect();
              setHover({ x: ev.clientX - r.left + wrap.current!.scrollLeft, y: ev.clientY - r.top, text: describe(e) });
            }} />
        ))}
      </svg>
      {hover && (
        <div className="tooltip" style={{ left: Math.min(hover.x + 12, width - 320), top: hover.y + 14 }}>{hover.text}</div>
      )}
    </div>
  );
}

export function Legend() {
  return (
    <div className="legend" aria-label="Timeline legend">
      {TIMELINE_LEGEND.map((l) => (
        <span key={l.label} className="legend-item">
          <svg width="14" height="14" aria-hidden="true">
            {l.shape === "tick" && <line x1="7" x2="7" y1="1" y2="13" stroke={l.color} strokeWidth="2" strokeLinecap="round" />}
            {l.shape === "dot" && <circle cx="7" cy="7" r="4" fill={l.color} />}
            {l.shape === "ring" && <circle cx="7" cy="7" r="4.5" fill="none" stroke={l.color} strokeWidth="2" />}
            {l.shape === "square" && <rect x="3" y="3" width="8" height="8" fill="none" stroke={l.color} strokeWidth="2" />}
            {l.shape === "x" && <g stroke={l.color} strokeWidth="2.5"><line x1="2" y1="2" x2="12" y2="12" /><line x1="2" y1="12" x2="12" y2="2" /></g>}
            {l.shape === "diamond" && <path d="M7 1 L12 7 L7 13 L2 7 Z" fill={l.color} />}
          </svg>
          {l.label}
        </span>
      ))}
      <span className="legend-item"><svg width="14" height="14" aria-hidden="true"><line x1="7" x2="7" y1="0" y2="14" stroke="var(--status-critical)" strokeWidth="2" /></svg>violation trigger</span>
    </div>
  );
}
