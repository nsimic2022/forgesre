import { useState } from "react";
import type React from "react";

/*
 * One host metric as a tile, in the style of a Grafana stat + time-series panel: the current value
 * large and coloured by state, the change over the window, the line with an area under it, the alarm
 * zone, gaps where no samples came, incident marks, a cursor shared by all tiles, and min / avg / max.
 * Drawing only: the numbers are the ones the metrics API already returns.
 */

export type MetricLine = { name: string; className?: string; series: number[]; times?: number[] };
export type MetricTileData = {
  key: string; name: string; display: string; tone: string; threshold: number | null; series: number[]; times: number[];
  unit?: string; kind?: string; value?: number | null; lines?: MetricLine[]; warn?: number | null; crit?: number | null; query?: string;
};
export type MetricWindow = { start: number; end: number; marker: number | null; label: string };
export type MetricMark = { at: number; tone: string; label: string };

export const TONE: Record<string, string> = { ok: "var(--ok)", warn: "var(--warn)", crit: "var(--crit)", high: "var(--high)", off: "var(--faint)" };
const SECOND = ["var(--accent)", "var(--muted)", "var(--high)", "var(--faint)"];
const SMALL = { W: 240, H: 84 }, LARGE = { W: 900, H: 220 };

type Pt = [number, number];

function points(series: number[], times: number[] | undefined): Pt[] {
  return series.map((v, i) => [times?.[i] ?? NaN, v] as Pt).filter(([t, v]) => Number.isFinite(t) && Number.isFinite(v));
}

/** Split a line where samples are missing (a gap over 3× the usual step), so no data never looks flat. */
export function segments(pts: Pt[]): Pt[][] {
  if (pts.length < 2) return pts.length ? [pts] : [];
  const steps = pts.slice(1).map((p, i) => p[0] - pts[i][0]).sort((a, b) => a - b);
  const usual = steps[Math.floor(steps.length / 2)] || 1;
  const out: Pt[][] = [[pts[0]]];
  for (let i = 1; i < pts.length; i++) {
    if (pts[i][0] - pts[i - 1][0] > usual * 3) out.push([]);
    out[out.length - 1].push(pts[i]);
  }
  return out;
}

/** Up/down as runs of the same state, so the strip is one bar per stretch, not one per sample. */
function runs(parts: Pt[][]): { from: number; to: number; up: boolean }[] {
  const out: { from: number; to: number; up: boolean }[] = [];
  for (const seg of parts) {
    seg.forEach(([t, v], i) => {
      const up = v >= 1, end = seg[i + 1]?.[0] ?? t;
      const prev = out[out.length - 1];
      if (i && prev && prev.up === up) prev.to = end;
      else out.push({ from: t, to: end, up });
    });
  }
  return out;
}

/** "−24 h", "−90 min": how far back the window starts. */
export function since(seconds: number): string {
  if (seconds >= 2 * 86400) return `−${Math.round(seconds / 86400)} d`;
  if (seconds >= 2 * 3600) return `−${Math.round(seconds / 3600)} h`;
  return `−${Math.max(1, Math.round(seconds / 60))} min`;
}

function unitOf(tile: MetricTileData): string {
  if (tile.unit) return tile.unit;
  if (tile.kind === "percent") return "%";
  return "";
}

function fmt(tile: MetricTileData, v: number): string {
  if (tile.kind === "load") return v.toFixed(2);
  if (tile.kind === "number") return Math.abs(v) >= 10 || Number.isInteger(v) ? v.toFixed(0) : v.toFixed(1);
  if (tile.kind === "percent") return v.toFixed(Math.abs(v) < 10 ? 1 : 0);
  return Math.abs(v) >= 100 ? v.toFixed(0) : v.toFixed(1);
}

/** Shorter number for the min / avg / max line, which has to fit a narrow tile. */
function short(tile: MetricTileData, v: number): string {
  if (tile.kind === "load") return v.toFixed(1);
  return fmt(tile, v);
}

/** The display string split into number and unit: "71%" → ["71", "%"], "48.2 MB/s" → ["48.2", "MB/s"]. */
function bigValue(display: string): [string, string] {
  const m = /^\s*([-+]?[\d.,]+)\s*(.*)$/.exec(display || "");
  return m ? [m[1], m[2]] : [display || "—", ""];
}

/** Grafana Explore with the same PromQL and time range (datasource uid "prometheus" from provisioning). */
export function grafanaLink(base: string, expr: string, start: number, end: number): string {
  const panes = { a: { datasource: "prometheus", queries: [{ refId: "A", expr, datasource: { type: "prometheus", uid: "prometheus" } }],
    range: { from: String(Math.round(start * 1000)), to: String(Math.round(end * 1000)) } } };
  return `${base}/explore?schemaVersion=1&orgId=1&panes=${encodeURIComponent(JSON.stringify(panes))}`;
}

function nearest(pts: Pt[], t: number): Pt | null {
  let best: Pt | null = null;
  for (const p of pts) if (!best || Math.abs(p[0] - t) < Math.abs(best[0] - t)) best = p;
  return best;
}

export function MetricTile({ tile, window, hover, onHover, marks = [], expanded = false, onToggle, onZoom, grafana = "" }: {
  tile: MetricTileData; window?: MetricWindow; hover: number | null; onHover: (t: number | null) => void; marks?: MetricMark[];
  expanded?: boolean; onToggle?: () => void; onZoom?: (start: number, end: number) => void; grafana?: string;
}) {
  const { W, H } = expanded ? LARGE : SMALL;
  const [drag, setDrag] = useState<{ from: number; to: number } | null>(null);
  // Only the samples inside the window (a zoom narrows it), so lines never spill out of the tile.
  const inside = (pts: Pt[]) => (window ? pts.filter(([t]) => t >= window.start && t <= window.end) : pts);
  const lines: { name: string; pts: Pt[] }[] = tile.lines && tile.lines.length > 1
    ? tile.lines.map((l) => ({ name: l.name, pts: inside(points(l.series, l.times ?? tile.times)) }))
    : [{ name: tile.name, pts: inside(points(tile.series, tile.times)) }];
  const main = lines[0]?.pts ?? [];
  const unit = unitOf(tile);
  const [num, numUnit] = bigValue(tile.display);
  const t0 = window?.start ?? main[0]?.[0] ?? 0;
  const t1 = window?.end ?? main[main.length - 1]?.[0] ?? 1;
  const isUp = tile.kind === "up";
  // Amber when the value is getting close to the alarm line, red when over it.
  // Colour follows the value now (the big number), also when a zoom shows an older stretch.
  const last = tile.value ?? (main.length ? main[main.length - 1][1] : null);
  const critLine = tile.crit ?? tile.threshold;
  const warnLine = tile.warn ?? null;
  const near = critLine != null && last != null && tile.tone !== "crit" && (warnLine != null ? last >= warnLine && last < critLine : tile.tone === "ok" && last >= critLine * 0.85);
  const tone = warnLine != null && critLine != null && last != null ? (last >= critLine ? "crit" : near ? "warn" : "ok") : near ? "warn" : tile.tone;
  const color = TONE[tone] ?? "var(--accent)";

  if (main.length < 2) {
    return (
      <div className="metric-tile">
        <div className="metric-head"><span>{tile.name}</span></div>
        <div className="metric-value" data-tone={tone} style={{ color }}>{num}{numUnit ? <span className="metric-unit"> {numUnit}</span> : null}</div>
        <span className="small faint">No samples in this window.</span>
      </div>
    );
  }

  const all = lines.flatMap((l) => l.pts.map((p) => p[1]));
  let lo = Math.min(...all, critLine ?? Infinity), hi = Math.max(...all, critLine ?? -Infinity);
  if (isUp) [lo, hi] = [0, 1];
  const pad = (hi - lo) * 0.08 || Math.abs(hi) * 0.1 || 1;
  if (!isUp) {
    lo -= pad;
    hi += pad;
    if (tile.kind === "percent" || tile.kind === "bytes" || tile.kind === "load") lo = Math.max(0, lo);
    if (tile.kind === "percent") hi = Math.min(100, Math.max(hi, lo + 1));
  }
  const x = (t: number) => ((t - t0) / Math.max(1, t1 - t0)) * W;
  const y = (v: number) => H - ((v - lo) / Math.max(1e-9, hi - lo)) * H;
  const path = (pts: Pt[]) => pts.map(([t, v], i) => `${i ? "L" : "M"}${x(t).toFixed(1)},${y(v).toFixed(1)}`).join(" ");
  const parts = segments(main);
  const values = main.map((p) => p[1]);
  const min = Math.min(...values), max = Math.max(...values), avg = values.reduce((a, b) => a + b, 0) / values.length;
  const delta = values[values.length - 1] - values[0];
  const cursor = hover != null && hover >= t0 && hover <= t1 ? hover : null;
  const at = cursor != null ? nearest(main, cursor) : null;
  const span = t1 - t0;
  const zoomed = window?.label === "zoomed";
  const timeAt = (e: React.MouseEvent<SVGSVGElement>) => {
    const r = e.currentTarget.getBoundingClientRect();
    return t0 + Math.min(1, Math.max(0, (e.clientX - r.left) / Math.max(1, r.width))) * span;
  };
  const move = (e: React.MouseEvent<SVGSVGElement>) => {
    const t = timeAt(e);
    onHover(t);
    if (drag) setDrag({ ...drag, to: t });
  };
  // Drag across the chart to zoom every tile into that stretch (like Grafana); a plain click does nothing.
  const up = () => {
    if (drag && onZoom && Math.abs(drag.to - drag.from) > span * 0.02) onZoom(Math.min(drag.from, drag.to), Math.max(drag.from, drag.to));
    setDrag(null);
  };
  const deltaColor = critLine != null ? (delta > 0 ? "var(--high)" : "var(--ok)") : "var(--faint)";
  const th = critLine;
  const warn = warnLine;

  return (
    <div className={`metric-tile${expanded ? " wide" : ""}`}>
      <div className="metric-head">
        {onToggle ? (
          <button type="button" className="metric-name" onClick={onToggle} aria-expanded={expanded} title={expanded ? "Make small again" : "Open large, with axes and zoom"}>
            {tile.name} <span aria-hidden="true">{expanded ? "⤡" : "⤢"}</span>
          </button>
        ) : <span>{tile.name}</span>}
        {!isUp && Math.abs(delta) > 1e-9 ? (
          <span style={{ color: deltaColor }} title={`Change over the window, ${since(span).slice(1)}`}>
            {delta > 0 ? "▲" : "▼"} {fmt(tile, Math.abs(delta))}{unit === "%" ? " %" : unit ? ` ${unit}` : ""}
          </span>
        ) : null}
      </div>
      <div className="metric-value" data-tone={tone} style={{ color }}>{num}{numUnit ? <span className="metric-unit"> {numUnit}</span> : null}</div>
      {lines.length > 1 ? (
        <div className="metric-legend">
          {lines.map((l, i) => <span key={l.name}><i style={{ background: i ? SECOND[(i - 1) % SECOND.length] : color }} />{l.name}</span>)}
        </div>
      ) : null}
      <svg viewBox={`0 -4 ${W} ${H + (expanded ? 24 : 8)}`} width="100%" role="img" aria-label={`${tile.name} over the window, now ${tile.display}`}
        onMouseMove={move} onMouseLeave={() => (onHover(null), setDrag(null))} onMouseDown={(e) => { const t = timeAt(e); setDrag({ from: t, to: t }); }} onMouseUp={up}
        style={{ display: "block", overflow: "visible", cursor: "crosshair", userSelect: "none" }}>
        {isUp ? (
          runs(parts).map((r, i) => (
            <rect key={i} x={x(r.from)} y={H * 0.3} width={Math.max(1, x(r.to) - x(r.from))} height={H * 0.4} rx={2}
              style={{ fill: r.up ? "var(--ok)" : "var(--crit)", opacity: 0.75 }}><title>{r.up ? "up" : "down"} {clockOf(r.from)}–{clockOf(r.to)}</title></rect>
          ))
        ) : (
          <>
            {th != null && th < hi ? <rect x={0} y={y(hi)} width={W} height={Math.max(0, y(th) - y(hi))} style={{ fill: "var(--crit)", opacity: 0.1 }} /> : null}
            {th != null && warn != null && warn < th ? <rect x={0} y={y(th)} width={W} height={Math.max(0, y(warn) - y(th))} style={{ fill: "var(--warn)", opacity: 0.08 }} /> : null}
            <line x1={0} x2={W} y1={H / 2} y2={H / 2} style={{ stroke: "var(--border)" }} />
            <line x1={0} x2={W} y1={H} y2={H} style={{ stroke: "var(--border)" }} />
            {th != null && th >= lo && th <= hi ? <line x1={0} x2={W} y1={y(th)} y2={y(th)} strokeDasharray="4 3" strokeWidth={1} style={{ stroke: "var(--crit)", opacity: 0.8 }} /> : null}
            {parts.map((seg, i) => seg.length > 1 ? (
              <path key={`a${i}`} d={`${path(seg)} L${x(seg[seg.length - 1][0]).toFixed(1)},${H} L${x(seg[0][0]).toFixed(1)},${H} Z`} style={{ fill: color, opacity: 0.13 }} />
            ) : null)}
            {lines.slice(1).map((l, li) => segments(l.pts).map((seg, i) => (
              <path key={`l${li}-${i}`} d={path(seg)} fill="none" strokeWidth={1.3} strokeDasharray="3 2" style={{ stroke: SECOND[li % SECOND.length] }} />
            )))}
            {parts.map((seg, i) => <path key={`p${i}`} d={path(seg)} fill="none" strokeWidth={1.8} strokeLinejoin="round" style={{ stroke: color }} />)}
            <circle cx={x(main[main.length - 1][0])} cy={y(main[main.length - 1][1])} r={3} style={{ fill: color }} />
          </>
        )}
        {window?.marker != null && window.marker >= t0 && window.marker <= t1 ? (
          <line x1={x(window.marker)} x2={x(window.marker)} y1={-4} y2={H} strokeWidth={1.2} style={{ stroke: "var(--text)", opacity: 0.6 }}><title>Incident start</title></line>
        ) : null}
        {marks.filter((m) => m.at >= t0 && m.at <= t1).map((m, i) => (
          <g key={i}>
            <line x1={x(m.at)} x2={x(m.at)} y1={0} y2={H} strokeDasharray="2 3" style={{ stroke: TONE[m.tone] ?? "var(--faint)", opacity: 0.7 }} />
            <path d={`M${x(m.at) - 4},-4 L${x(m.at) + 4},-4 L${x(m.at)},2 Z`} style={{ fill: TONE[m.tone] ?? "var(--faint)" }}><title>{m.label}</title></path>
          </g>
        ))}
        {drag && drag.to !== drag.from ? <rect x={x(Math.min(drag.from, drag.to))} y={-4} width={Math.abs(x(drag.to) - x(drag.from))} height={H + 4} style={{ fill: "var(--accent)", opacity: 0.15 }} /> : null}
        {expanded && !isUp ? [hi, (hi + lo) / 2, lo].map((v, i) => (
          <text key={`y${i}`} x={4} y={y(v) + (i === 0 ? 12 : i === 2 ? -4 : 4)} style={{ fill: "var(--faint)", fontSize: 11 }}>{fmt(tile, v)}{unit === "%" ? "%" : ""}</text>
        )) : null}
        {expanded ? [0.25, 0.5, 0.75].map((f) => (
          <g key={`t${f}`}>
            <line x1={W * f} x2={W * f} y1={0} y2={H} style={{ stroke: "var(--border)" }} />
            <text x={W * f} y={H + 14} textAnchor="middle" style={{ fill: "var(--faint)", fontSize: 11 }}>{clockOf(t0 + span * f)}</text>
          </g>
        )) : null}
        {cursor != null ? <line x1={x(cursor)} x2={x(cursor)} y1={-4} y2={H} strokeWidth={1} style={{ stroke: "var(--muted)" }} /> : null}
        {at && cursor != null && !isUp ? <circle cx={x(at[0])} cy={y(at[1])} r={3} style={{ fill: "var(--bg, #000)", stroke: color, strokeWidth: 1.5 }} /> : null}
      </svg>
      {at && cursor != null ? (
        <div className="metric-foot" style={{ justifyContent: "center" }}>
          <span className="metric-tip">{clockOf(at[0])} · {isUp ? (at[1] >= 1 ? "up" : "down") : `${fmt(tile, at[1])}${unit ? ` ${unit}` : ""}`}</span>
        </div>
      ) : (
        <div className="metric-foot">
          <span>{zoomed ? clockOf(t0) : since(span)}</span>
          <span>{zoomed || t1 < Date.now() / 1000 - 120 ? clockOf(t1) : "now"}</span>
        </div>
      )}
      {expanded ? (
        <div className="metric-foot" style={{ justifyContent: "flex-start", gap: 14 }}>
          {th != null ? <span>{warn != null ? `warning ${fmt(tile, warn)} · ` : ""}alarm {fmt(tile, th)}{unit ? ` ${unit}` : ""}</span> : null}
          <span>Drag across the chart to zoom.</span>
          {grafana && tile.query ? <a href={grafanaLink(grafana, tile.query, t0, t1)} target="_blank" rel="noreferrer">Open in Grafana</a> : null}
        </div>
      ) : null}
      <div className="metric-foot">
        {isUp ? <span>{tile.key === "ups_mains" ? "on mains" : "available"} {(avg * 100).toFixed(avg >= 0.999 ? 0 : 1)}%</span> : (
          <>
            <span>min {short(tile, min)}</span>
            <span>avg {short(tile, avg)}</span>
            <span>max {short(tile, max)}</span>
          </>
        )}
      </div>
    </div>
  );
}

function clockOf(t: number): string {
  const d = new Date(t * 1000);
  return `${String(d.getHours()).padStart(2, "0")}:${String(d.getMinutes()).padStart(2, "0")}`;
}
