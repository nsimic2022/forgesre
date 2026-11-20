import { useEffect, useState } from "react";
import { Link } from "react-router-dom";
import { getJson } from "../lib/api";
import { clock } from "../lib/format";
import { Card, Chip } from "./ui";

type Tile = { key: string; name: string; display: string; tone: string; threshold: number | null; series: number[]; times: number[]; unit?: string };
type Panel = { collecting: boolean; collecting_line: string; error: string; tiles: Tile[]; window?: { start: number; end: number; marker: number | null; label: string } };
export type Person = { role: string; name: string; phone: string; email: string; note?: string };
export type Similar = { number: string; status: string; started_at: string | null; minutes: number | null; root_cause: string; resolved_by: string };
export type Event = { at: string; kind: string; who: string; text: string };

const TONE: Record<string, string> = { ok: "var(--ok)", warn: "var(--warn)", crit: "var(--crit)", high: "var(--high)" };

/** One metric as a line over the incident window, with a vertical line where the incident started. */
function Spark({ tile, window }: { tile: Tile; window?: Panel["window"] }) {
  const W = 260, H = 56;
  const pts = tile.series.map((v, i) => [tile.times[i], v] as const).filter(([t, v]) => Number.isFinite(t) && Number.isFinite(v));
  if (pts.length < 2) return <span className="small faint">No samples in this window.</span>;
  const t0 = window?.start ?? pts[0][0], t1 = window?.end ?? pts[pts.length - 1][0];
  const lo = Math.min(...pts.map((p) => p[1]), tile.threshold ?? Infinity), hi = Math.max(...pts.map((p) => p[1]), tile.threshold ?? -Infinity);
  const x = (t: number) => ((t - t0) / Math.max(1, t1 - t0)) * W;
  const y = (v: number) => H - 4 - ((v - lo) / Math.max(1e-9, hi - lo)) * (H - 8);
  const path = pts.map(([t, v], i) => `${i ? "L" : "M"}${x(t).toFixed(1)},${y(v).toFixed(1)}`).join(" ");
  const color = TONE[tile.tone] ?? "var(--accent)";
  return (
    <svg viewBox={`0 0 ${W} ${H}`} width="100%" height={H} role="img" aria-label={`${tile.name} over the incident, now ${tile.display}`}>
      {tile.threshold !== null && tile.threshold !== undefined && hi > lo ? <line x1={0} x2={W} y1={y(tile.threshold)} y2={y(tile.threshold)} stroke="var(--crit)" strokeDasharray="3 3" strokeWidth={1} opacity={0.6} /> : null}
      {window?.marker ? <line x1={x(window.marker)} x2={x(window.marker)} y1={0} y2={H} stroke="var(--faint)" strokeWidth={1} /> : null}
      <path d={path} fill="none" stroke={color} strokeWidth={1.8} />
    </svg>
  );
}

/** Host metrics from one hour before the incident to now (or to the resolve time). */
export function IncidentMetrics({ assetId, number }: { assetId: string; number: string }) {
  const [panel, setPanel] = useState<Panel | null>(null);
  const [error, setError] = useState("");
  useEffect(() => {
    getJson<Panel>(`/api/v1/assets/${encodeURIComponent(assetId)}/metrics?incident=${encodeURIComponent(number)}`).then(setPanel).catch((e: Error) => setError(e.message));
  }, [assetId, number]);
  const tiles = (panel?.tiles ?? []).filter((t) => t.series?.length);
  return (
    <Card title="Host metrics" actions={panel?.window?.label ? <span className="small faint">{panel.window.label} · line = start</span> : null}>
      {error ? <span className="small" style={{ color: "var(--crit)" }}>{error}</span> : null}
      {!panel && !error ? <span className="muted">Reading Prometheus…</span> : null}
      {panel && !tiles.length ? <span className="muted">{panel.error || panel.collecting_line || "No metrics for this asset."}</span> : null}
      <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fill, minmax(240px, 1fr))", gap: 14 }}>
        {tiles.map((t) => (
          <div key={t.key} className="cell-stack" style={{ gap: 4 }}>
            <div className="row" style={{ justifyContent: "space-between" }}><span className="small faint">{t.name}</span><strong style={{ color: TONE[t.tone] ?? "var(--text)" }}>{t.display}</strong></div>
            <Spark tile={t} window={panel?.window} />
          </div>
        ))}
      </div>
    </Card>
  );
}

export function WhoToCall({ people }: { people: Person[] }) {
  return (
    <Card title="Who to call">
      {people.length === 0 ? <span className="muted">Nobody set: add a contact to the asset or an on-call schedule.</span> : null}
      {people.map((p, i) => (
        <div key={i} className="cell-stack" style={{ gap: 2, borderTop: i ? "1px solid var(--border)" : undefined, paddingTop: i ? 8 : 0 }}>
          <span className="small faint">{p.role}</span>
          <strong>{p.name || "—"}</strong>
          <div className="row small" style={{ gap: 12 }}>
            {p.phone ? <a href={`tel:${p.phone.replace(/\s+/g, "")}`}>{p.phone}</a> : null}
            {p.email ? <a href={`mailto:${p.email}`}>{p.email}</a> : null}
          </div>
          {p.note ? <span className="small muted">{p.note}</span> : null}
        </div>
      ))}
    </Card>
  );
}

export function SimilarBefore({ rows }: { rows: Similar[] }) {
  if (!rows.length) return null;
  return (
    <Card title={`Happened before on this asset · ${rows.length}`}>
      {rows.map((r) => (
        <div key={r.number} className="cell-stack" style={{ gap: 2, borderTop: "1px solid var(--border)", paddingTop: 8 }}>
          <div className="row" style={{ justifyContent: "space-between" }}>
            <Link className="mono" to={`/incidents/${encodeURIComponent(r.number)}`}>{r.number.split("_")[0]}</Link>
            <span className="small faint">{clock(r.started_at)}{r.minutes !== null ? ` · ${r.minutes} min` : ""}</span>
          </div>
          {r.root_cause ? <span className="small"><span className="faint">Cause: </span>{r.root_cause}</span> : <span className="small faint">No confirmed cause recorded.</span>}
        </div>
      ))}
    </Card>
  );
}

const KIND_TONE: Record<string, "crit" | "ok" | "info" | "off" | "warn"> = { alarm: "crit", ack: "info", resolve: "ok", notify: "off", note: "warn", work: "info", system: "off" };

/** Who did what, in order: the alarm, every mail and chat message, ack, notes, playbook steps, resolve. */
export function IncidentStory({ events }: { events: Event[] }) {
  return (
    <Card title="Who did what">
      <ol style={{ listStyle: "none", margin: 0, padding: 0 }}>
        {events.map((e, i) => (
          <li key={i} style={{ display: "grid", gridTemplateColumns: "64px 76px minmax(0, 1fr)", gap: 10, padding: "6px 0", borderTop: i ? "1px solid var(--border)" : undefined, alignItems: "baseline" }}>
            <span className="mono small faint">{clock(e.at)}</span>
            <Chip tone={KIND_TONE[e.kind] ?? "off"}>{e.kind}</Chip>
            <span className="small">{e.text}{e.who ? <span className="faint"> · {e.who}</span> : null}</span>
          </li>
        ))}
      </ol>
    </Card>
  );
}

/** Device syslog from Loki around the start, loaded on demand. */
export function SyslogCard({ number }: { number: string }) {
  const [data, setData] = useState<{ lines: string[]; note: string } | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const load = () => {
    setBusy(true);
    getJson<{ lines: string[]; note: string }>(`/api/v1/incidents/${encodeURIComponent(number)}/syslog`).then(setData).catch((e: Error) => setError(e.message)).finally(() => setBusy(false));
  };
  return (
    <Card title="Syslog around the start" actions={<button className="btn" type="button" onClick={load} disabled={busy}>{busy ? "Reading Loki…" : data ? "Refresh" : "Show syslog"}</button>}>
      {error ? <span className="small" style={{ color: "var(--crit)" }}>{error}</span> : null}
      {data?.note ? <span className="muted small">{data.note}</span> : null}
      {data?.lines.length ? <pre className="mono small" style={{ margin: 0, whiteSpace: "pre-wrap", maxHeight: 320, overflow: "auto" }}>{data.lines.join("\n")}</pre> : null}
      {!data && !error ? <span className="small faint">30 minutes either side of the start, from the device&apos;s own log.</span> : null}
    </Card>
  );
}
