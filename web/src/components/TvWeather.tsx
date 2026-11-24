import type { TvDay, TvHour, TvTile } from "../lib/api";

/*
 * Wall-screen pieces in the style of a weather report: an "icon" for the worst incident of a day,
 * a week strip, a slim 24-hour curve with shaded maintenance, and small tiles with one coloured word.
 */

const SEV_COLOR: Record<string, string> = { CRITICAL: "var(--crit)", HIGH: "var(--high)", WARNING: "var(--warn)", QUIET: "var(--ok)" };
const TONE_COLOR: Record<string, string> = { ok: "var(--ok)", warn: "var(--warn)", crit: "var(--crit)", info: "var(--accent2, var(--accent))", off: "var(--faint)" };

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

const TREND_ICON: Record<string, string> = { up: "↗", down: "↘", steady: "→" };

/* ---- One-screen wall layout (pages/Tv.tsx): compact versions of the pieces above. ---- */

/** Seven small day cells in one row: day, icon for the worst incident, count, criticals. A click opens that day's clock. */
export function WeekStrip({ days, picked, onPick }: { days: TvDay[]; picked?: string | null; onPick?: (date: string | null) => void }) {
  return (
    <div className="tvw-week" aria-label="Incidents per day, last 7 days">
      {days.map((d, i) => (
        <button type="button" key={d.date} className={`tvw-wday${i === days.length - 1 ? " today" : ""}${picked === d.date ? " picked" : ""}`}
          aria-pressed={picked === d.date} onClick={() => onPick?.(picked === d.date ? null : d.date)}
          aria-label={`${d.label}: ${d.incidents} incidents, ${d.critical} critical. Show this day's clock`}>
          <span className="tvw-wlabel">{i === days.length - 1 ? "Today" : d.label.slice(0, 3)}</span>
          <SevIcon worst={d.worst} size={40} />
          <span className="tvw-wcount">{d.incidents}</span>
          <span className="tvw-wcrit" style={{ color: d.critical ? "var(--crit)" : undefined }}>{d.critical ? `${d.critical} crit` : "·"}</span>
        </button>
      ))}
    </div>
  );
}

/**
 * The last 24 hours as a strip: the curve of incidents open per hour, and under it a bar per hour of
 * new incidents by severity with the count on top, so busy hours stand out. Maintenance is shaded
 * and named, the peak is marked, and a dashed line shows the current hour.
 */
export function HourlyStrip({ points, maintenance, from, to }: { points: TvHour[]; maintenance: { from: string; to: string; summary: string }[]; from: string; to: string }) {
  const SW = 1200;
  const SH = 100;
  const max = Math.max(4, ...points.map((p) => p.open));
  const maxNew = Math.max(3, ...points.map((p) => p.new));
  const step = SW / points.length;
  const x = (i: number) => step * i + step / 2;
  const y = (v: number) => SH - 6 - ((SH - 18) * v) / max;
  const barH = (v: number) => ((SH * 0.45) * v) / maxNew;
  const line: [number, number][] = points.map((p, i) => [x(i), y(p.open)]);
  const t0 = new Date(from).getTime();
  const span = Math.max(1, new Date(to).getTime() + 3_600_000 - t0);
  const tx = (iso: string) => ((new Date(iso).getTime() - t0) / span) * SW;
  const total = points.reduce((s, p) => s + p.new, 0);
  const crit = points.reduce((s, p) => s + p.critical, 0);
  const peak = Math.max(0, ...points.map((p) => p.open));
  const peakAt = points.findIndex((p) => p.open === peak);
  const fast = points.map((p) => p.acked_fast_pct).filter((n): n is number => n !== null);
  const bw = step * 0.5;
  return (
    <div className="tvw-strip">
      <div className="tvw-strip-head">
        <span>Last 24 hours</span>
        <span className="tvw-strip-legend">
          <span><i style={{ background: "var(--accent2, var(--accent))" }} />open</span>
          <span><i style={{ background: "var(--crit)" }} />critical</span>
          <span><i style={{ background: "var(--high)" }} />high</span>
          <span><i style={{ background: "var(--warn)" }} />other</span>
          <span className="faint">{total} new{crit ? ` · ${crit} critical` : ""} · peak {peak} open{fast.length ? ` · ${Math.round(fast.reduce((a, b) => a + b, 0) / fast.length)}% acked ≤15 min` : ""}{maintenance.length ? " · shaded: maintenance" : ""}</span>
        </span>
      </div>
      <svg viewBox={`0 0 ${SW} ${SH}`} preserveAspectRatio="none" role="img"
        aria-label={`Incidents open per hour, last 24 hours: ${total} new, ${crit} critical, at most ${peak} open at once`}>
        <defs>
          <linearGradient id="tvw-area" x1="0" y1="0" x2="0" y2="1">
            <stop offset="0%" stopColor="var(--accent2, var(--accent))" stopOpacity={0.45} />
            <stop offset="100%" stopColor="var(--accent2, var(--accent))" stopOpacity={0.04} />
          </linearGradient>
        </defs>
        {points.map((p, i) => (i % 2 ? <rect key={`z${p.at}`} x={step * i} y={0} width={step} height={SH} fill="var(--s2)" opacity={0.35} /> : null))}
        {maintenance.map((m, i) => (
          <rect key={i} x={tx(m.from)} y={0} width={Math.max(4, tx(m.to) - tx(m.from))} height={SH} fill="var(--accent2, var(--accent))" opacity={0.16}><title>Maintenance: {m.summary}</title></rect>
        ))}
        <line x1={0} x2={SW} y1={y(0)} y2={y(0)} stroke="var(--border)" />
        <line x1={0} x2={SW} y1={y(peak)} y2={y(peak)} stroke="var(--border)" strokeDasharray="4 6" vectorEffect="non-scaling-stroke" />
        <path d={`${smooth(line)} L${x(points.length - 1)},${SH} L${x(0)},${SH} Z`} fill="url(#tvw-area)" />
        {points.map((p, i) => {
          if (!p.new) return null;
          const other = Math.max(0, p.new - p.critical - p.high);
          let top = SH - 6;
          const seg = (v: number, color: string, k: string) => {
            if (!v) return null;
            const h = barH(v);
            top -= h;
            return <rect key={k} x={x(i) - bw / 2} y={top} width={bw} height={h} fill={color} rx={2} />;
          };
          return (
            <g key={`b${p.at}`}>
              {seg(other, "var(--warn)", "o")}{seg(p.high, "var(--high)", "h")}{seg(p.critical, "var(--crit)", "c")}
              <title>{`${String(p.hour).padStart(2, "0")}:00 · ${p.new} new (${p.critical} critical, ${p.high} high, ${other} other) · ${p.open} open`}</title>
            </g>
          );
        })}
        <path d={smooth(line)} fill="none" stroke="var(--accent2, var(--accent))" strokeWidth={2.5} vectorEffect="non-scaling-stroke" />
        {peak > 0 && peakAt >= 0 ? <circle cx={x(peakAt)} cy={y(peak)} r={4} fill="var(--accent2, var(--accent))" stroke="var(--bg)" strokeWidth={2} vectorEffect="non-scaling-stroke" /> : null}
        <line x1={x(points.length - 1)} x2={x(points.length - 1)} y1={0} y2={SH} stroke="var(--text)" strokeOpacity={0.5} strokeDasharray="3 4" vectorEffect="non-scaling-stroke" />
      </svg>
      <div className="tvw-strip-counts" aria-hidden="true">
        {points.map((p) => <span key={p.at} className={p.critical ? "crit" : p.high ? "high" : ""}>{p.new || ""}</span>)}
      </div>
      <div className="tvw-strip-hours" aria-hidden="true">
        {points.map((p, i) => <span key={p.at}>{i === points.length - 1 ? "now" : i % 3 === 0 ? `${String(p.hour).padStart(2, "0")}:00` : ""}</span>)}
      </div>
    </div>
  );
}

/** A small tile for the bottom row: title, big value, one coloured word. The sentence is the tooltip. */
export function MiniTile({ tile }: { tile: TvTile }) {
  const color = TONE_COLOR[tile.tone] ?? "var(--text)";
  const value = String(tile.value);
  return (
    <div className={`tvw-mini ${tile.tone}${value.length > 8 ? " long" : ""}`} title={[tile.sentence, ...tile.detail.filter(Boolean)].join("\n")}
      aria-label={`${tile.title}: ${tile.value} ${tile.unit}, ${tile.word}`}>
      <span className="tvw-mini-title">{tile.title}</span>
      <span className="tvw-mini-value">{value}{tile.unit ? <small>{tile.unit}</small> : null}</span>
      <span className="tvw-mini-word" style={{ color }}>{tile.word}<span aria-hidden="true"> {TREND_ICON[tile.trend]}</span></span>
    </div>
  );
}
