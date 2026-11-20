import type { ReactNode } from "react";
import type { TvDay, TvHour, TvTile } from "../lib/api";

/*
 * TV mode pieces in the style of a weather report: day cards with an "icon" for the day's worst
 * incident, a 24-hour curve with shaded maintenance bands and a row of drops, and small tiles with
 * a gauge, arc or bars, a status word with a trend badge and one sentence.
 */

const SEV_COLOR: Record<string, string> = { CRITICAL: "var(--crit)", HIGH: "var(--high)", WARNING: "var(--warn)", QUIET: "var(--ok)" };
const TONE_COLOR: Record<string, string> = { ok: "var(--ok)", warn: "var(--warn)", crit: "var(--crit)", info: "var(--tvw-accent2)", off: "var(--faint)" };

/** A small "weather icon" for a day or hour: a calm disc, or a cloud tinted by the worst severity. */
export function SevIcon({ worst, size = 64 }: { worst: string; size?: number }) {
  const color = SEV_COLOR[worst] ?? "var(--faint)";
  const calm = worst === "QUIET";
  return (
    <svg width={size} height={size} viewBox="0 0 64 64" aria-hidden="true">
      <defs>
        <radialGradient id={`sun-${worst}`} cx="40%" cy="35%" r="70%">
          <stop offset="0%" stopColor="#ffe08a" />
          <stop offset="100%" stopColor={calm ? "#f59f00" : color} />
        </radialGradient>
        <linearGradient id="cloud" x1="0" y1="0" x2="0" y2="1">
          <stop offset="0%" stopColor="#f1f5ff" />
          <stop offset="100%" stopColor="#9fb3d9" />
        </linearGradient>
      </defs>
      <circle cx={calm ? 32 : 40} cy={calm ? 32 : 22} r={calm ? 20 : 13} fill={`url(#sun-${worst})`} />
      {calm ? null : (
        <>
          <path d="M14 46a10 10 0 0 1 3-19 13 13 0 0 1 25-2 9 9 0 0 1 8 15 6 6 0 0 1-5 6Z" fill="url(#cloud)" />
          {worst === "CRITICAL" || worst === "HIGH" ? <path d="M28 46l-4 10h5l-3 8 9-12h-5l3-6Z" fill={color} /> : <path d="M30 48c-2 4-3 6-3 8a3 3 0 0 0 6 0c0-2-1-4-3-8Z" fill={color} />}
        </>
      )}
    </svg>
  );
}

/** One day card: date and label, the icon, incidents big and criticals small (like high / low). */
export function DayCard({ d, today }: { d: TvDay; today: boolean }) {
  return (
    <div className={`tvw-day${today ? " today" : ""}`} aria-label={`${d.label}: ${d.incidents} incidents, ${d.critical} critical`}>
      <div className="row" style={{ justifyContent: "space-between" }}><span className="tvw-daynum">{d.day}</span><span className="muted">{d.label}</span></div>
      <div className="row" style={{ justifyContent: "space-between", alignItems: "center" }}>
        <SevIcon worst={d.worst} size={today ? 84 : 66} />
        <div className="cell-stack" style={{ alignItems: "flex-end", gap: 4 }}>
          <span className="tvw-big">{d.incidents}</span>
          <span className="muted" style={{ color: d.critical ? "var(--crit)" : undefined }}>{d.critical} crit</span>
        </div>
      </div>
    </div>
  );
}

const W = 1200;
const H = 300;
const TOP = 70;
const BOTTOM = 250;

/** Smooth path through points (Catmull-Rom as cubic Béziers). */
function smooth(pts: [number, number][]): string {
  if (pts.length < 2) return "";
  let d = `M${pts[0][0]},${pts[0][1]}`;
  for (let i = 0; i < pts.length - 1; i++) {
    const [p0, p1, p2, p3] = [pts[i - 1] ?? pts[i], pts[i], pts[i + 1], pts[i + 2] ?? pts[i + 1]];
    d += ` C${p1[0] + (p2[0] - p0[0]) / 6},${p1[1] + (p2[1] - p0[1]) / 6} ${p2[0] - (p3[0] - p1[0]) / 6},${p2[1] - (p3[1] - p1[1]) / 6} ${p2[0]},${p2[1]}`;
  }
  return d;
}

/** 24 hours: incidents open per hour as a shaded curve, every other hour labelled with its new incidents, maintenance shaded, drops = acked within 15 min. */
export function HourlyChart({ points, maintenance, from, to }: { points: TvHour[]; maintenance: { from: string; to: string; summary: string }[]; from: string; to: string }) {
  const max = Math.max(4, ...points.map((p) => p.open));
  const step = W / points.length;
  const x = (i: number) => step * i + step / 2;
  const y = (v: number) => BOTTOM - ((BOTTOM - TOP) * v) / max;
  const line: [number, number][] = points.map((p, i) => [x(i), y(p.open)]);
  const t0 = new Date(from).getTime();
  const span = Math.max(1, new Date(to).getTime() + 3_600_000 - t0);
  const tx = (iso: string) => ((new Date(iso).getTime() - t0) / span) * W;
  const worst = (p: TvHour) => (p.critical ? "CRITICAL" : p.high ? "HIGH" : p.new ? "WARNING" : "QUIET");
  const total = points.reduce((s, p) => s + p.new, 0);
  return (
    <div className="tvw-chart">
      <svg viewBox={`0 0 ${W} ${H}`} role="img" aria-label={`Incidents open per hour, last 24 hours: ${total} new, at most ${Math.max(0, ...points.map((p) => p.open))} open at once`} preserveAspectRatio="none">
        <defs>
          <linearGradient id="tvw-fill" x1="0" y1="0" x2="1" y2="0">
            <stop offset="0%" stopColor="#6fbf8f" stopOpacity="0.85" />
            <stop offset="60%" stopColor="#5fb3b3" stopOpacity="0.8" />
            <stop offset="100%" stopColor="#e0a63c" stopOpacity="0.8" />
          </linearGradient>
          <linearGradient id="tvw-fade" x1="0" y1="0" x2="0" y2="1">
            <stop offset="0%" stopColor="#000" stopOpacity="0" />
            <stop offset="100%" stopColor="var(--tvw-bg)" stopOpacity="0.9" />
          </linearGradient>
        </defs>
        {maintenance.map((m, i) => (
          <rect key={i} x={tx(m.from)} y={TOP - 10} width={Math.max(4, tx(m.to) - tx(m.from))} height={BOTTOM - TOP + 10} fill="var(--tvw-band)"><title>Maintenance: {m.summary}</title></rect>
        ))}
        {[0, 0.5, 1].map((f) => <line key={f} x1={0} x2={W} y1={y(max * f)} y2={y(max * f)} stroke="var(--tvw-grid)" strokeDasharray="4 6" />)}
        <path d={`${smooth(line)} L${x(points.length - 1)},${BOTTOM} L${x(0)},${BOTTOM} Z`} fill="url(#tvw-fill)" />
        <path d={`M0,${TOP} H${W} V${BOTTOM} H0 Z`} fill="url(#tvw-fade)" />
        <path d={smooth(line)} fill="none" stroke="#d8f3e4" strokeWidth={2} strokeOpacity={0.8} />
      </svg>
      <div className="tvw-hours">
        {points.map((p, i) => (i % 2 === 1 ? null : (
          <div key={p.at} className="cell-stack" style={{ alignItems: "center", gap: 2 }}>
            <span className="muted">{i === points.length - 2 ? "Now" : `${String(p.hour).padStart(2, "0")}:00`}</span>
            <SevIcon worst={worst(p)} size={30} />
            <span>{p.new + (points[i + 1]?.new ?? 0)}</span>
          </div>
        )))}
      </div>
      <div className="tvw-drops" aria-label="Share of new incidents acknowledged within 15 minutes">
        {points.map((p, i) => (i % 2 === 1 ? null : (
          <span key={p.at} className={p.acked_fast_pct === null && (points[i + 1]?.acked_fast_pct ?? null) === null ? "none" : ""}>
            {(() => {
              const v = [p.acked_fast_pct, points[i + 1]?.acked_fast_pct].filter((n): n is number => n !== null);
              return v.length ? <><i className="tvw-drop" aria-hidden="true" />{Math.round(v.reduce((a, b) => a + b, 0) / v.length)}%</> : null;
            })()}
          </span>
        )))}
      </div>
    </div>
  );
}

const TREND_ICON: Record<string, string> = { up: "↗", down: "↘", steady: "→" };

function Gauge({ value, color, children }: { value: number; color: string; children: ReactNode }) {
  // 270° arc from bottom-left to bottom-right, like the UV and AQI dials
  const r = 54;
  const len = Math.PI * 2 * r * 0.75;
  return (
    <div className="tvw-gauge">
      <svg viewBox="0 0 140 140" aria-hidden="true">
        <defs>
          <linearGradient id="tvw-arc" x1="0" y1="1" x2="1" y2="0">
            <stop offset="0%" stopColor="var(--ok)" /><stop offset="50%" stopColor="var(--warn)" /><stop offset="100%" stopColor="var(--crit)" />
          </linearGradient>
        </defs>
        <circle cx="70" cy="70" r={r} fill="none" stroke="var(--tvw-grid)" strokeWidth="10" strokeDasharray={`${len} ${2 * Math.PI * r}`} transform="rotate(135 70 70)" strokeLinecap="round" />
        <circle cx="70" cy="70" r={r} fill="none" stroke={color === "gradient" ? "url(#tvw-arc)" : color} strokeWidth="10" strokeDasharray={`${len * value} ${2 * Math.PI * r}`} transform="rotate(135 70 70)" strokeLinecap="round" />
      </svg>
      <div className="tvw-gauge-value">{children}</div>
    </div>
  );
}

function ShiftArc({ part }: { part: number }) {
  const p = Math.max(0, Math.min(1, part));
  const a = Math.PI * (1 - p);
  return (
    <svg viewBox="0 0 200 110" className="tvw-arc" aria-hidden="true">
      <defs>
        <linearGradient id="tvw-sun" x1="0" x2="1"><stop offset="0%" stopColor="#c0392b" /><stop offset="100%" stopColor="#6c3fb5" /></linearGradient>
      </defs>
      <line x1="10" x2="190" y1="96" y2="96" stroke="var(--tvw-grid)" />
      <path d="M20 96 A80 80 0 0 1 180 96" fill="none" stroke="url(#tvw-sun)" strokeWidth="5" />
      <circle cx={100 + 80 * Math.cos(a)} cy={96 - 80 * Math.sin(a)} r="9" fill="#fff" />
    </svg>
  );
}

function Bars({ bars }: { bars: { label: string; value: number }[] }) {
  const max = Math.max(1, ...bars.map((b) => b.value));
  if (!bars.length) return <span className="muted">Quiet</span>;
  return (
    <div className="tvw-bars">
      {bars.map((b) => (
        <div key={b.label} style={{ display: "contents" }}>
          <span className="small" style={{ overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>{b.label}</span>
          <span className="tvw-track"><span className="tvw-bar" style={{ width: `${Math.max(6, (b.value / max) * 100)}%` }} /></span>
          <span className="small muted">{b.value}</span>
        </div>
      ))}
    </div>
  );
}

function Columns({ part }: { part: number }) {
  const lit = Math.round(Math.max(0, Math.min(1, part)) * 7);
  return (
    <div className="row" style={{ gap: 8, alignItems: "flex-end", height: 110 }} aria-hidden="true">
      {Array.from({ length: 7 }, (_, i) => <span key={i} className={`tvw-col${i < lit ? " on" : ""}`} />)}
    </div>
  );
}

/** One small tile; a click (or Enter) turns it over to show the detail lines. */
export function Tile({ tile, open, onToggle }: { tile: TvTile; open: boolean; onToggle: () => void }) {
  const color = TONE_COLOR[tile.tone] ?? "var(--text)";
  const big = (
    <span className="tvw-value">{tile.value}<span className="tvw-unit">{tile.unit}</span></span>
  );
  let visual: ReactNode = big;
  if (tile.key === "shift") visual = <div className="cell-stack" style={{ alignItems: "center", gap: 0 }}><ShiftArc part={tile.gauge ?? 0} /><span className="row" style={{ gap: 18 }}><span className="tvw-time">{tile.start ?? ""}<small>start</small></span><span className="tvw-time">{tile.end ?? ""}<small>end</small></span></span></div>;
  else if (tile.key === "noisy") visual = <Bars bars={tile.bars ?? []} />;
  else if (tile.key === "hosts") visual = <div className="row" style={{ gap: 18, alignItems: "flex-end" }}><Columns part={tile.gauge ?? 0} />{big}</div>;
  else if (tile.gauge !== null) visual = <Gauge value={tile.gauge} color={tile.key === "slo" || tile.key === "backup" ? color : "gradient"}>{big}</Gauge>;
  return (
    <button type="button" className={`tvw-tile${open ? " open" : ""}${tile.key === "noisy" ? " wide" : ""}`} onClick={onToggle} aria-expanded={open} aria-label={`${tile.title}: ${tile.value} ${tile.unit}, ${tile.word}`}>
      <span className="tvw-tile-title">{tile.title}</span>
      {open ? (
        <ul className="tvw-detail">{tile.detail.filter(Boolean).length ? tile.detail.filter(Boolean).map((d, i) => <li key={i}>{d}</li>) : <li>No more detail.</li>}</ul>
      ) : <div className="tvw-visual">{visual}</div>}
      {tile.key === "shift" ? <span className="tvw-person">{tile.value}</span> : null}
      <span className="tvw-word">{tile.word} <span className="tvw-trend" style={{ background: color }} aria-label={`trend ${tile.trend}`}>{TREND_ICON[tile.trend]}</span></span>
      <span className="tvw-sentence">{tile.sentence}</span>
    </button>
  );
}
