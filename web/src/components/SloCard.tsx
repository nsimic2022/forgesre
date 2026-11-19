import { useEffect, useState, type FormEvent } from "react";
import { getJson, postJson, putJson } from "../lib/api";
import { Card, Chip } from "./ui";

export type SloRow = {
  id: number; name: string; scope: string; scope_value: string; target: number; window_days: number; severities: string[]; owner: string;
  availability: number; downtime_min: number; budget_min: number; budget_left_min: number; budget_used_pct: number;
  burn_rate_24h: number; incidents: number; status: "ok" | "at_risk" | "breached"; worst: { asset: string; downtime_min: number }[];
};
const STATUS = { ok: ["ok", "Within budget"], at_risk: ["high", "Over 75 % used"], breached: ["crit", "Budget spent"] } as const;
const mins = (m: number) => (Math.abs(m) < 60 ? `${Math.round(m)} min` : `${(m / 60).toFixed(1)} h`);

function SloForm({ row, onDone }: { row: SloRow | null; onDone: (saved: boolean) => void }) {
  const [d, setD] = useState({ name: row?.name ?? "", scope: row?.scope ?? "all", scope_value: row?.scope_value ?? "", target: String(row?.target ?? 99.9), window_days: String(row?.window_days ?? 30), severities: row?.severities ?? ["CRITICAL"], owner: row?.owner ?? "" });
  const [error, setError] = useState("");
  const save = (e: FormEvent) => {
    e.preventDefault();
    const body = { ...d, target: Number(d.target), window_days: Number(d.window_days) };
    (row ? putJson(`/api/v1/slos/${row.id}`, body) : postJson("/api/v1/slos", body)).then(() => onDone(true)).catch((err: Error) => setError(err.message));
  };
  const field = (key: "name" | "scope_value" | "target" | "window_days" | "owner", label: string, extra: object = {}) => (
    <label className="cell-stack small faint" style={{ flex: "1 1 150px" }}>{label}<input className="search" style={{ minWidth: 0 }} value={d[key]} onChange={(e) => setD({ ...d, [key]: e.target.value })} {...extra} /></label>
  );
  return (
    <form onSubmit={save} className="cell-stack" style={{ gap: 10, borderTop: "1px solid var(--border)", paddingTop: 12 }}>
      <div className="row" style={{ alignItems: "flex-end" }}>
        {field("name", "Name", { placeholder: "ERP database" })}
        <label className="cell-stack small faint" style={{ flex: "1 1 130px" }}>Covers
          <select className="search" style={{ minWidth: 0 }} value={d.scope} onChange={(e) => setD({ ...d, scope: e.target.value })}>
            <option value="all">All assets</option><option value="asset">One asset</option><option value="type">Asset type</option><option value="site">Site</option>
          </select>
        </label>
        {d.scope !== "all" ? field("scope_value", d.scope === "asset" ? "Hostname, id or IP" : d.scope === "type" ? "Type (e.g. Oracle DB)" : "Site") : null}
      </div>
      <div className="row" style={{ alignItems: "flex-end" }}>
        {field("target", "Target %", { type: "number", step: "0.01", min: 50, max: 99.999 })}
        {field("window_days", "Window (days)", { type: "number", min: 1, max: 365 })}
        {field("owner", "Owner (optional)")}
      </div>
      <div className="row" role="group" aria-label="Counts incidents of">
        <span className="small faint">Counts</span>
        {["CRITICAL", "HIGH", "WARNING"].map((s) => (
          <label key={s} className="btn"><input type="checkbox" checked={d.severities.includes(s)} onChange={() => setD({ ...d, severities: d.severities.includes(s) ? d.severities.filter((x) => x !== s) : [...d.severities, s] })} /> {s.toLowerCase()}</label>
        ))}
      </div>
      {error ? <div className="banner error" role="alert">{error}</div> : null}
      <div className="row">
        <button className="btn primary" type="submit" disabled={!d.name.trim()}>Save SLO</button>
        <button className="btn" type="button" onClick={() => onDone(false)}>Cancel</button>
      </div>
    </form>
  );
}

/** Reports › SLOs: availability against target, error budget, 24 h burn; Engineer edits. */
export function SloCard() {
  const [rows, setRows] = useState<SloRow[] | null>(null);
  const [canEdit, setCanEdit] = useState(false);
  const [edit, setEdit] = useState<{ row: SloRow | null } | null>(null);
  const [error, setError] = useState("");
  const load = () => getJson<{ slos: SloRow[]; can_edit: boolean }>("/api/v1/slos").then((d) => (setRows(d.slos), setCanEdit(d.can_edit))).catch((e: Error) => setError(e.message));
  useEffect(() => {
    load();
  }, []);
  const done = (saved: boolean) => (setEdit(null), saved && load());
  return (
    <Card title="SLOs · error budget" actions={canEdit ? <button className="btn primary" type="button" onClick={() => setEdit({ row: null })}>New SLO</button> : null}>
      {error ? <div className="banner error" role="alert">{error}</div> : null}
      {edit && edit.row === null ? <SloForm row={null} onDone={done} /> : null}
      {rows && rows.length === 0 ? <span className="muted">No SLO yet. A good first one: 99.9 % over 30 days for the systems people use, counting critical incidents.</span> : null}
      {(rows ?? []).map((r) => {
        const [tone, label] = STATUS[r.status];
        const used = Math.min(100, Math.max(0, r.budget_used_pct));
        return (
          <div key={r.id} className="cell-stack" style={{ borderTop: "1px solid var(--border)", paddingTop: 12, gap: 6 }}>
            <div className="row" style={{ justifyContent: "space-between" }}>
              <span className="row" style={{ gap: 8 }}><strong>{r.name}</strong><span className="small faint">{r.scope === "all" ? "all assets" : `${r.scope}: ${r.scope_value}`} · {r.window_days} d · {r.severities.join(", ").toLowerCase()}</span></span>
              <Chip tone={tone}>{label}</Chip>
            </div>
            <div className="row" style={{ gap: 18 }}>
              <span><span className="stat-value" style={{ fontSize: 24 }}>{r.availability.toFixed(3)}%</span> <span className="small faint">target {r.target}%</span></span>
              <span className="small muted">down {mins(r.downtime_min)} of {mins(r.budget_min)} allowed · {r.budget_left_min >= 0 ? `${mins(r.budget_left_min)} left` : `${mins(-r.budget_left_min)} over`}</span>
              <span className="small mono" style={{ color: r.burn_rate_24h > 1 ? "var(--high)" : "var(--faint)" }}>burn 24 h {r.burn_rate_24h}x</span>
            </div>
            <div className="bar" role="img" aria-label={`${r.budget_used_pct}% of the error budget used`}><span style={{ width: `${used}%`, background: `var(--${tone === "ok" ? "ok" : tone === "high" ? "high" : "crit"})` }} /></div>
            {r.worst.length ? <span className="small faint">Most downtime: {r.worst.map((w) => `${w.asset} ${mins(w.downtime_min)}`).join(" · ")}</span> : null}
            {canEdit ? (
              <div className="row" style={{ gap: 6 }}>
                <button className="btn" type="button" onClick={() => setEdit({ row: r })} aria-label={`Edit SLO ${r.name}`}>Edit</button>
                <button className="btn" type="button" onClick={() => getJson(`/api/v1/slos/${r.id}`, { method: "DELETE" }).then(load)} aria-label={`Remove SLO ${r.name}`}>Remove</button>
              </div>
            ) : null}
            {edit?.row?.id === r.id ? <SloForm row={r} onDone={done} /> : null}
          </div>
        );
      })}
    </Card>
  );
}
