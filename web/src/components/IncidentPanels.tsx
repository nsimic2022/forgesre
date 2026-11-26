import { useEffect, useState } from "react";
import { Link } from "react-router-dom";
import { getJson } from "../lib/api";
import { clock } from "../lib/format";
import { Card, Chip } from "./ui";
import { MetricTile, type MetricMark, type MetricTileData, type MetricWindow } from "./MetricTile";

type Panel = { collecting: boolean; collecting_line: string; error: string; tiles: MetricTileData[]; window?: MetricWindow; grafana?: string };
export type Person = { role: string; name: string; phone: string; email: string; note?: string };
export type Similar = { number: string; status: string; started_at: string | null; minutes: number | null; root_cause: string; resolved_by: string };
export type Event = { at: string; kind: string; who: string; text: string };

const PERIODS = [[1, "1 h"], [6, "6 h"], [24, "24 h"], [168, "7 d"]] as const;

/** Host metrics: with an incident, from one hour before it to now (or to the resolve time); without one,
 *  the chosen period (1 h to 7 days). Tiles share one cursor and one zoom (drag across any chart), and a
 *  tile's name opens it large with axes and an "Open in Grafana" link. */
export function IncidentMetrics({ assetId, number = "", tileWidth = 240, marks = [] }: { assetId: string; number?: string; tileWidth?: number; marks?: MetricMark[] }) {
  const [panel, setPanel] = useState<Panel | null>(null);
  const [error, setError] = useState("");
  const [hover, setHover] = useState<number | null>(null);
  const [hours, setHours] = useState(24);
  const [zoom, setZoom] = useState<{ start: number; end: number } | null>(null);
  const [open, setOpen] = useState("");
  useEffect(() => {
    setZoom(null);
    const q = number ? `incident=${encodeURIComponent(number)}` : `hours=${hours}`;
    getJson<Panel>(`/api/v1/assets/${encodeURIComponent(assetId)}/metrics?${q}`).then((p) => (setPanel(p), setError(""))).catch((e: Error) => setError(e.message));
  }, [assetId, number, hours]);
  const tiles = (panel?.tiles ?? []).filter((t) => t.series?.length);
  const window = panel?.window && zoom ? { ...panel.window, ...zoom, label: "zoomed" } : panel?.window;
  return (
    <Card title="Host metrics" actions={
      <>
        {zoom ? <button className="btn" type="button" onClick={() => setZoom(null)}>Reset zoom</button> : null}
        {number ? (panel?.window?.label ? <span className="small faint">{panel.window.label} · line = start</span> : null) : (
          <span className="seg" role="group" aria-label="Period">
            {PERIODS.map(([h, label]) => (
              <button key={h} type="button" className={`btn${hours === h ? " on" : ""}`} aria-pressed={hours === h} onClick={() => setHours(h)}>{label}</button>
            ))}
          </span>
        )}
      </>
    }>
      {error ? <span className="small" style={{ color: "var(--crit)" }}>{error}</span> : null}
      {!panel && !error ? <span className="muted">Reading Prometheus…</span> : null}
      {panel && !tiles.length ? <span className="muted">{panel.error || panel.collecting_line || "No metrics for this asset."}</span> : null}
      <div className="metric-grid" style={{ gridTemplateColumns: `repeat(auto-fill, minmax(${tileWidth}px, 1fr))` }}>
        {tiles.map((t) => (
          <MetricTile key={t.key} tile={t} window={window} hover={hover} onHover={setHover} marks={marks} grafana={panel?.grafana ?? ""}
            expanded={open === t.key} onToggle={() => setOpen(open === t.key ? "" : t.key)} onZoom={(start, end) => setZoom({ start, end })} />
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
