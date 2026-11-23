import { useEffect, useState } from "react";
import { Link } from "react-router-dom";
import { getJson } from "../lib/api";
import { Card, Chip } from "./ui";

type RackDevice = { asset_id: string; hostname: string; type: string; u: number | null; height: number; status: string; incident: string };
type Rack = { site: string; row: string; rack: string; units: number; devices: RackDevice[]; unplaced: RackDevice[]; used_u: number; overlaps: string[]; too_high: string[] };
type RackData = { racks: Rack[]; units: number };
type LifeRow = { asset: string; hostname: string; kind: string; label: string; date: string; days_left: number; status: string };

const sevTone = (s: string) => (s === "CRITICAL" ? "var(--crit)" : s === "HIGH" ? "var(--high)" : s ? "var(--warn)" : "var(--accent)");

/** One rack drawn top-down: U numbers on the left, each device spans its height. */
export function RackElevation({ rack }: { rack: Rack }) {
  const unit = 16;
  const devices = Array.isArray(rack.devices) ? rack.devices : [];
  const unplaced = Array.isArray(rack.unplaced) ? rack.unplaced : [];
  const where = [rack.site, rack.row, rack.rack].filter(Boolean).join(" / ");
  return (
    <Card title={where} actions={<span className="small faint">{rack.used_u} of {rack.units} U</span>}>
      <div className="rack" role="list" aria-label={`Rack ${rack.rack}`}
        style={{ display: "grid", gridTemplateColumns: "34px 1fr", gridTemplateRows: `repeat(${rack.units}, ${unit}px)`, border: "1px solid var(--border)", borderRadius: 6 }}>
        {Array.from({ length: rack.units }, (_, i) => rack.units - i).map((u, i) => (
          <span key={u} className="small faint mono" style={{ gridColumn: 1, gridRow: i + 1, textAlign: "right", paddingRight: 6, fontSize: 10, lineHeight: `${unit}px`, borderTop: "1px dashed var(--border)" }}>{u}</span>
        ))}
        {devices.filter((d) => d.u !== null && d.u <= rack.units).map((d) => {
          const top = Math.min(rack.units, (d.u ?? 1) + d.height - 1);
          return (
            <Link key={d.asset_id} role="listitem" to={`/inventory?q=${encodeURIComponent(d.hostname)}`} title={`${d.hostname} · ${d.type}${d.incident ? ` · open ${d.incident}` : ""}`}
              style={{ gridColumn: 2, gridRow: `${rack.units - top + 1} / span ${top - (d.u ?? 1) + 1}`, margin: 1, padding: "0 8px", borderRadius: 4,
                background: "var(--s3)", borderLeft: `4px solid ${sevTone(d.incident)}`, overflow: "hidden", whiteSpace: "nowrap",
                textOverflow: "ellipsis", fontSize: 12, lineHeight: `${Math.max(1, d.height) * unit - 2}px`, color: "inherit", textDecoration: "none" }}>
              {d.hostname} <span className="faint">{d.type}</span>
            </Link>
          );
        })}
      </div>
      {unplaced.length ? <span className="small muted">No position: {unplaced.map((d) => d.hostname).join(", ")}</span> : null}
      {rack.overlaps?.length ? <span className="small" style={{ color: "var(--warn)" }}>Overlap: {rack.overlaps.join("; ")}</span> : null}
      {rack.too_high?.length ? <span className="small" style={{ color: "var(--warn)" }}>Above U{rack.units}: {rack.too_high.join(", ")}</span> : null}
    </Card>
  );
}

/** Inventory › Racks: every rack with its devices; a red or amber edge marks an open incident. */
export function RacksPage() {
  const [data, setData] = useState<RackData | null>(null);
  const [site, setSite] = useState("");
  const [error, setError] = useState("");
  useEffect(() => {
    document.title = "Racks · ForgeSRE";
    getJson<RackData>("/api/v1/racks").then(setData).catch((e: Error) => setError(e.message));
  }, []);
  const racks = Array.isArray(data?.racks) ? data!.racks : [];
  const sites = [...new Set(racks.map((r) => r.site).filter(Boolean))];
  const shown = racks.filter((r) => !site || r.site === site);
  return (
    <>
      <header className="page-head">
        <div>
          <h1>Racks</h1>
          <span className="small faint"><Link to="/inventory">Inventory</Link> / Racks · set Rack, Rack position and Height on the asset</span>
        </div>
        {sites.length > 1 ? (
          <select className="search" aria-label="Site" value={site} onChange={(e) => setSite(e.target.value)} style={{ width: 220 }}>
            <option value="">All sites</option>
            {sites.map((s) => <option key={s} value={s}>{s}</option>)}
          </select>
        ) : null}
      </header>
      {error ? <div className="banner error" role="alert">{error}</div> : null}
      {data && !racks.length ? <Card><span className="muted">No asset has a rack yet. Edit an asset › Location › Rack and Rack position.</span></Card> : null}
      <div className="row" style={{ alignItems: "flex-start", flexWrap: "wrap", gap: 16 }}>
        {shown.map((r) => <div key={`${r.site}/${r.row}/${r.rack}`} style={{ flex: "0 1 320px", minWidth: 260 }}><RackElevation rack={r} /></div>)}
      </div>
    </>
  );
}

/** Reports: support / warranty, end of life and licence dates expiring or expired, soonest first. */
export function LifecycleCard() {
  const [data, setData] = useState<{ rows: LifeRow[]; tracked: number; warn_days: number } | null>(null);
  useEffect(() => void getJson<{ rows: LifeRow[]; tracked: number; warn_days: number }>("/api/v1/lifecycle").then(setData).catch(() => setData(null)), []);
  if (!data) return null;
  const rows = Array.isArray(data.rows) ? data.rows : [];
  return (
    <Card title="Warranty, end of life and licences" actions={<span className="small faint">{data.tracked} dates tracked</span>}>
      {rows.length === 0 ? <span className="muted">Nothing expires in the next {data.warn_days} days.</span> : (
        <table className="data">
          <thead><tr><th>Asset</th><th>What</th><th>Date</th><th>When</th></tr></thead>
          <tbody>
            {rows.map((r) => (
              <tr key={`${r.asset}-${r.kind}`}>
                <td className="mono">{r.hostname}</td>
                <td>{r.label}</td>
                <td className="mono">{r.date}</td>
                <td><Chip tone={r.status === "expired" ? "crit" : "warn"}>{r.days_left < 0 ? `${-r.days_left} d ago` : r.days_left === 0 ? "today" : `in ${r.days_left} d`}</Chip></td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </Card>
  );
}
