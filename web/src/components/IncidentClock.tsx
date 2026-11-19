import { useState } from "react";
import type { IncidentClockData } from "../lib/api";

const SIZE = 560;
const C = SIZE / 2;
const R_IN = 96;
const R_OUT = 214;
const RIM = 232;
const LAYERS = [
  { key: "critical", label: "Critical", color: "var(--crit)" },
  { key: "high", label: "High", color: "var(--high)" },
  { key: "warning", label: "Warning", color: "var(--warn)" },
] as const;

const TAU = Math.PI * 2;
/** Clock angle (0 = 12 o'clock, clockwise) for a time of day on a 12-hour dial. */
export const dialAngle = (hour: number, minute = 0) => (((hour % 12) + minute / 60) / 12) * TAU;
const pt = (a: number, r: number): [number, number] => [C + r * Math.sin(a), C - r * Math.cos(a)];

function wedge(a0: number, a1: number, r0: number, r1: number) {
  const large = a1 - a0 > Math.PI ? 1 : 0;
  const [x0, y0] = pt(a0, r1);
  const [x1, y1] = pt(a1, r1);
  const [x2, y2] = pt(a1, r0);
  const [x3, y3] = pt(a0, r0);
  return `M${x0},${y0}A${r1},${r1} 0 ${large} 1 ${x1},${y1}L${x2},${y2}A${r0},${r0} 0 ${large} 0 ${x3},${y3}Z`;
}

function arc(a0: number, a1: number, r: number) {
  const large = a1 - a0 > Math.PI ? 1 : 0;
  const [x0, y0] = pt(a0, r);
  const [x1, y1] = pt(a1, r);
  return `M${x0},${y0}A${r},${r} 0 ${large} 1 ${x1},${y1}`;
}

const hh = (n: number) => String(n).padStart(2, "0");

/**
 * The last 12 hours on a wall-clock dial: each hour sits where it is on a real clock, stacked by
 * severity (critical inside). The hand and the rim marker show the time now; the empty slice right
 * after the hand separates the newest hour from the oldest.
 */
export function IncidentClock({ clock, open, critical }: { clock: IncidentClockData; open: number; critical: number }) {
  const [hover, setHover] = useState<number | null>(null);
  const hand = dialAngle(clock.hour, clock.minute);
  const peak = Math.max(4, ...clock.hours.map((h) => h.critical + h.high + h.warning));
  const unit = (R_OUT - R_IN) / peak;
  const step = TAU / 12;
  const last = clock.hours.length - 1;
  const shown = hover === null ? null : clock.hours[hover];
  const summary = `${clock.total} incidents in the last 12 hours, now ${hh(clock.hour)}:${hh(clock.minute)}`;
  return (
    <div className="cell-stack" style={{ alignItems: "center", gap: 8 }}>
      <svg viewBox={`0 0 ${SIZE} ${SIZE}`} width="100%" style={{ maxWidth: 640 }} role="img" aria-label={summary}>
        <title>{summary}</title>
        <circle cx={C} cy={C} r={RIM} fill="none" stroke="var(--border)" strokeWidth={2} />
        {/* Window of the last 12 hours on the rim: from the oldest hour up to the hand. */}
        <path d={arc(dialAngle(clock.hours[0].hour), dialAngle(clock.hours[0].hour) + (TAU - step) + (clock.minute / 60) * step, RIM)} fill="none" stroke="var(--accent)" strokeOpacity={0.35} strokeWidth={6} strokeLinecap="round" />
        {clock.hours.map((h, i) => {
          const a0 = dialAngle(h.hour) + 0.012;
          const a1 = i === last ? Math.max(a0 + 0.02, hand - 0.012) : dialAngle(h.hour) + step - 0.012;
          let r = R_IN;
          return (
            <g key={h.start} onMouseEnter={() => setHover(i)} onMouseLeave={() => setHover(null)}>
              <path d={wedge(a0, a1, R_IN, R_OUT)} fill="var(--s2)" opacity={hover === i ? 1 : 0.55} />
              {LAYERS.map((layer) => {
                const n = h[layer.key];
                if (!n) return null;
                const d = wedge(a0, a1, r + 1, r + n * unit - 1);
                r += n * unit;
                return <path key={layer.key} d={d} fill={layer.color} />;
              })}
            </g>
          );
        })}
        {Array.from({ length: 12 }, (_, n) => {
          const [x, y] = pt(dialAngle(n), RIM + 22);
          return <text key={n} x={x} y={y + 6} textAnchor="middle" fontSize={17} fill="var(--faint)" fontFamily="var(--mono)">{n === 0 ? 12 : n}</text>;
        })}
        {/* Hand and rim marker for the time now. */}
        <line x1={pt(hand, R_IN - 8)[0]} y1={pt(hand, R_IN - 8)[1]} x2={pt(hand, RIM)[0]} y2={pt(hand, RIM)[1]} stroke="var(--text)" strokeWidth={3} strokeLinecap="round" />
        <circle cx={pt(hand, RIM)[0]} cy={pt(hand, RIM)[1]} r={9} fill="var(--accent)" stroke="var(--bg)" strokeWidth={3} />
        <text x={C} y={C - 6} textAnchor="middle" fontSize={54} fontWeight={700} fill="var(--text)">{open}</text>
        <text x={C} y={C + 24} textAnchor="middle" fontSize={17} fill="var(--muted)">open now</text>
        <text x={C} y={C + 50} textAnchor="middle" fontSize={16} fill={critical ? "var(--crit)" : "var(--faint)"}>{critical} critical</text>
      </svg>
      <div className="row" style={{ gap: 18, justifyContent: "center" }}>
        {LAYERS.map((layer) => (
          <span key={layer.key} className="row small" style={{ gap: 6 }}>
            <span aria-hidden="true" style={{ width: 12, height: 12, borderRadius: 3, background: layer.color }} />
            {layer.label}
          </span>
        ))}
        <span className="small faint mono">now {hh(clock.hour)}:{hh(clock.minute)} · {clock.total} in 12 h</span>
      </div>
      <div className="small" style={{ minHeight: 20 }} aria-live="polite">
        {shown ? `${hh(shown.hour)}:00–${hh((shown.hour + 1) % 24)}:00 · ${shown.critical} critical · ${shown.high} high · ${shown.warning} warning` : ""}
      </div>
      <table className="vh">
        <caption>Incidents per hour, last 12 hours</caption>
        <thead><tr><th>Hour</th><th>Critical</th><th>High</th><th>Warning</th></tr></thead>
        <tbody>{clock.hours.map((h) => <tr key={h.start}><td>{hh(h.hour)}:00</td><td>{h.critical}</td><td>{h.high}</td><td>{h.warning}</td></tr>)}</tbody>
      </table>
    </div>
  );
}
