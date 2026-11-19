import { useState, type FormEvent } from "react";
import { getJson, postJson, putJson, type NoiseFilter } from "../lib/api";
import { clock } from "../lib/format";
import { Card, Chip } from "./ui";

const DAYS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"];
const hm = (m: number | null) => (m === null ? "" : `${String(Math.floor(m / 60)).padStart(2, "0")}:${String(m % 60).padStart(2, "0")}`);
const minutes = (v: string) => (v ? Number(v.slice(0, 2)) * 60 + Number(v.slice(3, 5)) : null);

export function when(f: NoiseFilter): string {
  if (f.action === "delay") return `first ${f.delay_minutes} min of each alarm`;
  const days = f.days.length && f.days.length < 7 ? f.days.map((d) => DAYS[d]).join(", ") : "every day";
  const window = f.start_minute === null ? "all day" : `${hm(f.start_minute)}–${hm(f.end_minute)}`;
  return `${days}, ${window}${f.expires_at ? ` · until ${clock(f.expires_at)}` : ""}`;
}

type Draft = {
  name: string; alertnames: string; scope: NoiseFilter["scope"]; scope_value: string; days: number[];
  start: string; end: string; max_value: string; action: NoiseFilter["action"]; delay_minutes: string;
  expires_at: string; owner: string; note: string; enabled: boolean; confirm_always: boolean;
};
const EMPTY: Draft = { name: "", alertnames: "", scope: "all", scope_value: "", days: [], start: "", end: "", max_value: "", action: "quiet", delay_minutes: "5", expires_at: "", owner: "", note: "", enabled: true, confirm_always: false };

function toDraft(f: NoiseFilter): Draft {
  return { name: f.name, alertnames: f.alertnames.join(", "), scope: f.scope, scope_value: f.scope_value, days: f.days, start: hm(f.start_minute), end: hm(f.end_minute), max_value: f.max_value === null ? "" : String(f.max_value), action: f.action, delay_minutes: String(f.delay_minutes), expires_at: f.expires_at ? f.expires_at.slice(0, 16) : "", owner: f.owner, note: f.note, enabled: f.enabled, confirm_always: false };
}

function toBody(d: Draft) {
  return {
    name: d.name, enabled: d.enabled, alertnames: d.alertnames.split(",").map((s) => s.trim()).filter(Boolean),
    scope: d.scope, scope_value: d.scope_value, days: d.days, start_minute: minutes(d.start), end_minute: minutes(d.end),
    max_value: d.max_value === "" ? null : Number(d.max_value), action: d.action, delay_minutes: Number(d.delay_minutes || 5),
    expires_at: d.expires_at || null, owner: d.owner, note: d.note, confirm_always: d.confirm_always,
  };
}

function FilterForm({ initial, id, onDone }: { initial: Draft; id: number | null; onDone: () => void }) {
  const [d, setD] = useState<Draft>(initial);
  const [error, setError] = useState("");
  const set = (patch: Partial<Draft>) => setD({ ...d, ...patch });
  const save = (e: FormEvent) => {
    e.preventDefault();
    const call = id === null ? postJson("/api/v1/noise-filters", toBody(d)) : putJson(`/api/v1/noise-filters/${id}`, toBody(d));
    call.then(onDone).catch((err: Error) => setError(err.message));
  };
  const text = (key: keyof Draft, label: string, placeholder = "", type = "text") => (
    <label className="cell-stack small faint" style={{ flex: "1 1 180px" }}>
      {label}
      <input className="search" style={{ minWidth: 0 }} type={type} placeholder={placeholder} value={String(d[key])} onChange={(e) => set({ [key]: e.target.value } as Partial<Draft>)} />
    </label>
  );
  return (
    <Card title={id === null ? "New noise filter" : `Edit · ${initial.name}`}>
      <form onSubmit={save} className="cell-stack" style={{ gap: 12 }}>
        <div className="row" style={{ alignItems: "flex-end" }}>
          {text("name", "Name", "Backup window disk spike")}
          {text("alertnames", "Alerts (comma separated, empty = any)", "NodeFilesystemUsageHigh")}
        </div>
        <div className="row" style={{ alignItems: "flex-end" }}>
          <label className="cell-stack small faint">
            Applies to
            <select className="search" value={d.scope} onChange={(e) => set({ scope: e.target.value as Draft["scope"] })}>
              <option value="all">All assets</option>
              <option value="asset">One asset</option>
              <option value="type">Asset type</option>
              <option value="site">Site</option>
            </select>
          </label>
          {d.scope !== "all" ? text("scope_value", d.scope === "asset" ? "Hostname, id or IP" : d.scope === "type" ? "Type" : "Site") : null}
          {text("max_value", "Only below value (optional)", "97", "number")}
        </div>
        <fieldset style={{ border: 0, padding: 0, margin: 0 }}>
          <legend className="small faint">Then</legend>
          <div className="row">
            <label className="btn"><input type="radio" name="action" checked={d.action === "quiet"} onChange={() => set({ action: "quiet" })} /> Record, notify nobody</label>
            <label className="btn"><input type="radio" name="action" checked={d.action === "delay"} onChange={() => set({ action: "delay" })} /> Wait before opening</label>
          </div>
        </fieldset>
        {d.action === "delay" ? (
          <div className="row">{text("delay_minutes", "Minutes to wait", "5", "number")}</div>
        ) : (
          <>
            <fieldset style={{ border: 0, padding: 0, margin: 0 }}>
              <legend className="small faint">Days (none = every day)</legend>
              <div className="row">
                {DAYS.map((name, i) => (
                  <label key={name} className="btn">
                    <input type="checkbox" checked={d.days.includes(i)} onChange={() => set({ days: d.days.includes(i) ? d.days.filter((x) => x !== i) : [...d.days, i].sort() })} /> {name}
                  </label>
                ))}
              </div>
            </fieldset>
            <div className="row" style={{ alignItems: "flex-end" }}>
              {text("start", "From (empty = all day)", "", "time")}
              {text("end", "Until", "", "time")}
              {text("expires_at", "Filter ends on (optional)", "", "datetime-local")}
            </div>
            {!d.start && !d.days.length && !d.expires_at ? (
              <label className="row small" style={{ color: "var(--warn)" }}>
                <input type="checkbox" checked={d.confirm_always} onChange={(e) => set({ confirm_always: e.target.checked })} />
                I know this silences the alert at all times
              </label>
            ) : null}
          </>
        )}
        <div className="row" style={{ alignItems: "flex-end" }}>
          {text("owner", "Owner", "dba team")}
          {text("note", "Why (shown on the incident)", "nightly RMAN backup")}
        </div>
        <label className="row small"><input type="checkbox" checked={d.enabled} onChange={(e) => set({ enabled: e.target.checked })} /> Enabled</label>
        {error ? <div className="banner error" role="alert">{error}</div> : null}
        <div className="row">
          <button className="btn primary" type="submit" disabled={!d.name.trim()}>Save filter</button>
          <button className="btn" type="button" onClick={onDone}>Cancel</button>
        </div>
      </form>
    </Card>
  );
}

/** Noise filters tab of the Automation page (docs/alerting.md rule 7). */
export function NoiseFilters({ filters, canEdit, onChanged }: { filters: NoiseFilter[]; canEdit: boolean; onChanged: () => void }) {
  const [editing, setEditing] = useState<{ id: number | null; draft: Draft } | null>(null);
  const done = () => (setEditing(null), onChanged());
  const remove = (f: NoiseFilter) => {
    if (window.confirm(`Remove noise filter “${f.name}”? Alarms it quiets now will open and notify.`)) {
      getJson(`/api/v1/noise-filters/${f.id}`, { method: "DELETE" }).then(onChanged);
    }
  };
  return (
    <>
      <Card title="Noise filters" actions={canEdit ? <button className="btn primary" onClick={() => setEditing({ id: null, draft: EMPTY })}>New filter</button> : null}>
        <span className="small muted">A filtered alarm is still stored and shown on its incident, but nobody is notified while the filter applies. When it stops applying and the alarm is still firing, the incident opens and escalation starts.</span>
        <div className="table-wrap">
          <table className="data" style={{ minWidth: 980 }}>
            <thead>
              <tr><th style={{ width: 180 }}>Name</th><th style={{ width: 160 }}>Alerts</th><th style={{ width: 150 }}>Applies to</th><th>When</th><th style={{ width: 90 }}>Below</th><th style={{ width: 120 }}>Owner</th><th style={{ width: 70 }}>Hits</th><th style={{ width: 60 }}>On</th>{canEdit ? <th style={{ width: 140 }} /> : null}</tr>
            </thead>
            <tbody>
              {filters.map((f) => (
                <tr key={f.id}>
                  <td><div className="cell-stack"><span style={{ fontWeight: 600 }}>{f.name}</span>{f.note ? <span className="small muted">{f.note}</span> : null}</div></td>
                  <td className="mono small" style={{ wordBreak: "break-word", maxWidth: 200 }}>{f.alertnames.length ? f.alertnames.join(", ") : "any"}</td>
                  <td className="small">{f.scope === "all" ? "All assets" : `${f.scope}: ${f.scope_value}`}</td>
                  <td className="small">{when(f)}</td>
                  <td className="small muted">{f.max_value ?? "—"}</td>
                  <td className="small">{f.owner || "—"}</td>
                  <td className="small muted">{f.hits}</td>
                  <td><Chip tone={f.enabled ? "ok" : "off"}>{f.enabled ? "On" : "Off"}</Chip></td>
                  {canEdit ? (
                    <td><div className="row"><button className="btn" onClick={() => setEditing({ id: f.id, draft: toDraft(f) })}>Edit</button><button className="btn" onClick={() => remove(f)} aria-label={`Remove ${f.name}`}>Remove</button></div></td>
                  ) : null}
                </tr>
              ))}
            </tbody>
          </table>
          {filters.length === 0 ? <div className="empty">No noise filters yet. Parent-down suppression and maintenance windows already work without one.</div> : null}
        </div>
      </Card>
      {editing ? <FilterForm key={editing.id ?? "new"} id={editing.id} initial={editing.draft} onDone={done} /> : null}
    </>
  );
}
