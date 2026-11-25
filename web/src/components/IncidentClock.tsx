import { useEffect, useRef, useState } from "react";
import type { DayHour, IncidentClockData, TvDayDetail } from "../lib/api";

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

/** Hour and minute hands from the centre, with a dark edge so they read over the coloured hours. */
function Hands({ hour, minute }: { hour: number; minute: number }) {
  const hA = dialAngle(hour, minute);
  const mA = (minute / 60) * TAU;
  const hand = (a: number, len: number, w: number, color: string) => {
    const [x1, y1] = pt(a + Math.PI, 22);
    const [x2, y2] = pt(a, len);
    return (
      <g>
        <line x1={x1} y1={y1} x2={x2} y2={y2} stroke="var(--bg)" strokeWidth={w + 5} strokeLinecap="round" />
        <line x1={x1} y1={y1} x2={x2} y2={y2} stroke={color} strokeWidth={w} strokeLinecap="round" />
      </g>
    );
  };
  return (
    <g aria-hidden="true" className="clock-hands">
      {hand(hA, R_IN + 58, 11, "var(--text)")}
      {hand(mA, R_OUT + 6, 6, "var(--text)")}
      <circle cx={C} cy={C} r={13} fill="var(--accent)" stroke="var(--bg)" strokeWidth={4} />
      <circle cx={pt(mA, RIM)[0]} cy={pt(mA, RIM)[1]} r={7} fill="var(--accent)" stroke="var(--bg)" strokeWidth={3} />
    </g>
  );
}

/** Minute ticks on the rim (every 5 longer), like a wall clock. */
function Ticks() {
  return (
    <g aria-hidden="true">
      {Array.from({ length: 60 }, (_, m) => {
        const a = (m / 60) * TAU;
        const [x1, y1] = pt(a, RIM - (m % 5 ? 5 : 11));
        const [x2, y2] = pt(a, RIM + 1);
        return <line key={m} x1={x1} y1={y1} x2={x2} y2={y2} stroke="var(--faint)" strokeWidth={m % 5 ? 1.5 : 3} />;
      })}
    </g>
  );
}

/** The appliance time from the last refresh, moved on by the browser clock so the hands keep going. */
function useLiveTime(hour: number, minute: number): [number, number] {
  const since = useRef(Date.now());
  const [, tick] = useState(0);
  useEffect(() => {
    since.current = Date.now();
  }, [hour, minute]);
  useEffect(() => {
    const t = window.setInterval(() => tick((n) => n + 1), 10_000);
    return () => window.clearInterval(t);
  }, []);
  const total = hour * 60 + minute + Math.floor((Date.now() - since.current) / 60_000);
  return [Math.floor(total / 60) % 24, total % 60];
}

/**
 * The last 12 hours on a wall clock: each hour sits where it is on a real dial, stacked by severity
 * (critical inside) with the number of incidents written in it. Hour and minute hands show the time
 * now; the empty slice right after the hour hand separates the newest hour from the oldest.
 */
export function IncidentClock({ clock, open, critical }: { clock: IncidentClockData; open: number; critical: number }) {
  const [hover, setHover] = useState<number | null>(null);
  const [nowH, nowM] = useLiveTime(clock.hour, clock.minute);
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
          const count = h.critical + h.high + h.warning;
          const [lx, ly] = pt((a0 + a1) / 2, Math.min(R_OUT - 16, R_IN + count * unit + 18));
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
              {count ? <text x={lx} y={ly + 7} textAnchor="middle" fontSize={20} fontWeight={700} fill="var(--text)" stroke="var(--bg)" strokeWidth={3} paintOrder="stroke">{count}</text> : null}
            </g>
          );
        })}
        {Array.from({ length: 12 }, (_, n) => {
          const [x, y] = pt(dialAngle(n), RIM + 22);
          return <text key={n} x={x} y={y + 6} textAnchor="middle" fontSize={17} fill="var(--faint)" fontFamily="var(--mono)">{n === 0 ? 12 : n}</text>;
        })}
        <Ticks />
        {/* open now and critical sit above and below the hub, under the hands */}
        <text x={C} y={C - 50} textAnchor="middle" fontSize={34} fontWeight={700} fill="var(--text)" opacity={0.9}>{open}</text>
        <text x={C} y={C - 28} textAnchor="middle" fontSize={13} fill="var(--muted)">open now</text>
        <text x={C} y={C + 48} textAnchor="middle" fontSize={15} fill={critical ? "var(--crit)" : "var(--faint)"}>{critical} critical</text>
        <Hands hour={nowH} minute={nowM} />
      </svg>
      <div className="row" style={{ gap: 18, justifyContent: "center" }}>
        {LAYERS.map((layer) => (
          <span key={layer.key} className="row small" style={{ gap: 6 }}>
            <span aria-hidden="true" style={{ width: 12, height: 12, borderRadius: 3, background: layer.color }} />
            {layer.label}
          </span>
        ))}
        <span className="small faint mono">now {hh(nowH)}:{hh(nowM)} · {clock.total} in 12 h</span>
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

const sum = (h: DayHour) => h.critical + h.high + h.warning;

/**
 * One past day on the wall-clock dial, half a day at a time: ‹ AM on the left, PM › on the right.
 * Each hour is stacked by severity with its count written inside; the centre names the half and
 * its totals per severity. Starts on the busier half so a stormy afternoon shows first.
 */
export function DayClock({ day, onClose }: { day: TvDayDetail; onClose: () => void }) {
  const busyPm = day.pm.reduce((s, h) => s + sum(h), 0) > day.am.reduce((s, h) => s + sum(h), 0);
  const [half, setHalf] = useState<"am" | "pm">(busyPm ? "pm" : "am");
  const hours = day[half];
  const peak = Math.max(4, ...day.am.map(sum), ...day.pm.map(sum));
  const unit = (R_OUT - R_IN) / peak;
  const step = TAU / 12;
  const n = (k: "critical" | "high" | "warning") => hours.reduce((s, h) => s + h[k], 0);
  const total = n("critical") + n("high") + n("warning");
  const name = half === "am" ? "AM · 00–12" : "PM · 12–24";
  const summary = `${day.label} ${name}: ${total} incidents, ${n("critical")} critical, ${n("high")} high, ${n("warning")} warning`;
  return (
    <div className="cell-stack tvw-dayclock" style={{ alignItems: "center", gap: 8 }}>
      <div className="tvw-dayclock-head">
        <button type="button" className={`tvw-half${half === "am" ? " on" : ""}`} onClick={() => setHalf("am")} aria-pressed={half === "am"}>‹ AM</button>
        <strong>{day.label}</strong>
        <button type="button" className={`tvw-half${half === "pm" ? " on" : ""}`} onClick={() => setHalf("pm")} aria-pressed={half === "pm"}>PM ›</button>
        <button type="button" className="tvw-half close" onClick={onClose} aria-label="Back to the last 12 hours">✕</button>
      </div>
      <svg viewBox={`0 0 ${SIZE} ${SIZE}`} width="100%" style={{ maxWidth: 640 }} role="img" aria-label={summary}>
        <title>{summary}</title>
        <circle cx={C} cy={C} r={RIM} fill="none" stroke="var(--border)" strokeWidth={2} />
        {hours.map((h) => {
          const a0 = dialAngle(h.hour) + 0.012;
          const a1 = dialAngle(h.hour) + step - 0.012;
          let r = R_IN;
          const count = sum(h);
          const [lx, ly] = pt((a0 + a1) / 2, Math.min(R_OUT - 18, R_IN + count * unit + 20));
          return (
            <g key={h.hour}>
              <path d={wedge(a0, a1, R_IN, R_OUT)} fill="var(--s2)" opacity={0.55} />
              {LAYERS.map((layer) => {
                const v = h[layer.key];
                if (!v) return null;
                const d = wedge(a0, a1, r + 1, r + v * unit - 1);
                r += v * unit;
                return <path key={layer.key} d={d} fill={layer.color}><title>{`${hh(h.hour)}:00 ${v} ${layer.label.toLowerCase()}`}</title></path>;
              })}
              {count ? <text x={lx} y={ly + 7} textAnchor="middle" fontSize={20} fontWeight={700} fill="var(--text)">{count}</text> : null}
            </g>
          );
        })}
        {Array.from({ length: 12 }, (_, i) => {
          const [x, y] = pt(dialAngle(i), RIM + 22);
          const label = half === "am" ? (i === 0 ? 12 : i) : i + 12;
          return <text key={i} x={x} y={y + 6} textAnchor="middle" fontSize={17} fill="var(--faint)" fontFamily="var(--mono)">{label}</text>;
        })}
        <text x={C} y={C - 22} textAnchor="middle" fontSize={22} fill="var(--muted)">{half.toUpperCase()}</text>
        <text x={C} y={C + 20} textAnchor="middle" fontSize={50} fontWeight={700} fill="var(--text)">{total}</text>
        <text x={C} y={C + 48} textAnchor="middle" fontSize={16} fill="var(--muted)">{total ? "incidents" : "quiet"}</text>
      </svg>
      <div className="row tvw-daytotals" style={{ gap: 14, justifyContent: "center" }}>
        {LAYERS.map((layer) => (
          <span key={layer.key} className="tvw-daytotal" style={{ borderColor: layer.color, color: n(layer.key) ? layer.color : "var(--faint)" }}>
            {layer.label} <strong>{n(layer.key)}</strong>
          </span>
        ))}
      </div>
      <table className="vh">
        <caption>{`Incidents per hour, ${day.label} ${name}`}</caption>
        <thead><tr><th>Hour</th><th>Critical</th><th>High</th><th>Warning</th></tr></thead>
        <tbody>{hours.map((h) => <tr key={h.hour}><td>{hh(h.hour)}:00</td><td>{h.critical}</td><td>{h.high}</td><td>{h.warning}</td></tr>)}</tbody>
      </table>
    </div>
  );
}
