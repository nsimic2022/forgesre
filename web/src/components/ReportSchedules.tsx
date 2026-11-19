import { useState, type FormEvent } from "react";
import { getJson, postJson, putJson, type ScheduledReport } from "../lib/api";
import { clock } from "../lib/format";
import { Card, Chip } from "./ui";

const PRESETS: [string, string][] = [["15m", "Every 15 minutes"], ["60m", "Every hour"], ["6h", "Every 6 hours"], ["12h", "Every 12 hours"], ["24h", "Every day"], ["custom", "Custom interval"], ["cron", "Cron expression"], ["once", "Once"]];
const CRON_EXAMPLES: [string, string][] = [["0 7 * * *", "every day 07:00"], ["0 7 * * 1", "Mondays 07:00"], ["0 8 1 * *", "1st of the month 08:00"], ["30 6 * * 1-5", "weekdays 06:30"]];
const DAYS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"];

type Draft = { name: string; recipients: string; schedule: string; every_n: string; every_unit: string; weekdays: number[]; cron: string };

function toDraft(r: ScheduledReport | null): Draft {
  const s = r?.spec;
  const minutes = s?.every_minutes ?? 0;
  return {
    name: r?.name ?? "Weekly NOC summary", recipients: (r?.recipients ?? []).join(", "), schedule: s?.preset || (r ? "24h" : "cron"),
    every_n: s?.preset === "custom" ? String(minutes % 60 === 0 ? minutes / 60 : minutes) : "", every_unit: s?.preset === "custom" && minutes % 60 === 0 ? "hours" : "minutes",
    weekdays: s && s.weekdays.length < 7 ? s.weekdays : [], cron: s?.cron ?? (r ? "" : "0 7 * * 1"),
  };
}

function ScheduleForm({ report, onDone }: { report: ScheduledReport | null; onDone: (saved: boolean) => void }) {
  const [d, setD] = useState<Draft>(() => toDraft(report));
  const [error, setError] = useState("");
  const set = (patch: Partial<Draft>) => setD({ ...d, ...patch });
  const save = (e: FormEvent) => {
    e.preventDefault();
    const body = { ...d, recipients: d.recipients.split(/[,;\s]+/).filter(Boolean), asset_ids: report?.asset_ids ?? [] };
    const call = report ? putJson(`/api/v1/scheduled-reports/${report.id}`, body) : postJson("/api/v1/scheduled-reports", body);
    call.then(() => onDone(true)).catch((err: Error) => setError(err.message));
  };
  const usesDays = d.schedule !== "cron" && d.schedule !== "once";
  return (
    <form onSubmit={save} className="cell-stack" style={{ gap: 10, borderTop: "1px solid var(--border)", paddingTop: 10 }}>
      <label className="cell-stack small faint">Name<input className="search" style={{ minWidth: 0 }} value={d.name} onChange={(e) => set({ name: e.target.value })} /></label>
      <label className="cell-stack small faint">Recipients (comma separated)<input className="search" style={{ minWidth: 0 }} value={d.recipients} onChange={(e) => set({ recipients: e.target.value })} placeholder="noc@dc.local, boss@dc.local" /></label>
      <label className="cell-stack small faint">
        When
        <select className="search" style={{ minWidth: 0 }} value={d.schedule} onChange={(e) => set({ schedule: e.target.value })}>
          {PRESETS.map(([v, l]) => <option key={v} value={v}>{l}</option>)}
        </select>
      </label>
      {d.schedule === "custom" ? (
        <div className="row">
          <label className="cell-stack small faint" style={{ flex: "1 1 90px" }}>Every<input className="search" style={{ minWidth: 0 }} type="number" min={1} value={d.every_n} onChange={(e) => set({ every_n: e.target.value })} /></label>
          <label className="cell-stack small faint" style={{ flex: "1 1 90px" }}>Unit
            <select className="search" style={{ minWidth: 0 }} value={d.every_unit} onChange={(e) => set({ every_unit: e.target.value })}><option value="minutes">minutes</option><option value="hours">hours</option></select>
          </label>
        </div>
      ) : null}
      {d.schedule === "cron" ? (
        <>
          <label className="cell-stack small faint">Cron: minute hour day month weekday (appliance time)
            <input className="search mono" style={{ minWidth: 0 }} value={d.cron} onChange={(e) => set({ cron: e.target.value })} />
          </label>
          <div className="row" style={{ gap: 6 }}>
            {CRON_EXAMPLES.map(([expr, label]) => <button key={expr} type="button" className="btn" onClick={() => set({ cron: expr })} title={expr}>{label}</button>)}
          </div>
        </>
      ) : null}
      {usesDays ? (
        <div className="row" style={{ gap: 4 }} role="group" aria-label="Only on these days">
          {DAYS.map((name, i) => (
            <label key={name} className="btn"><input type="checkbox" checked={d.weekdays.includes(i)} onChange={() => set({ weekdays: d.weekdays.includes(i) ? d.weekdays.filter((x) => x !== i) : [...d.weekdays, i] })} /> {name}</label>
          ))}
        </div>
      ) : null}
      {error ? <div className="banner error" role="alert">{error}</div> : null}
      <div className="row">
        <button className="btn primary" type="submit" disabled={!d.recipients.trim()}>Save schedule</button>
        <button className="btn" type="button" onClick={() => onDone(false)}>Cancel</button>
      </div>
    </form>
  );
}

/** Scheduled reports card on the Reports page: list plus create, edit, on/off and remove. */
export function ReportSchedules({ rows, canEdit, onChanged }: { rows: ScheduledReport[]; canEdit: boolean; onChanged: () => void }) {
  const [editing, setEditing] = useState<{ report: ScheduledReport | null } | null>(null);
  const [asking, setAsking] = useState<number | null>(null);
  const [error, setError] = useState("");
  const act = (call: Promise<unknown>) => call.then(() => (setAsking(null), onChanged())).catch((e: Error) => setError(e.message));
  const done = (saved: boolean) => (setEditing(null), saved && onChanged());
  return (
    <Card title="Scheduled reports" actions={canEdit ? <button className="btn primary" type="button" onClick={() => setEditing({ report: null })}>New schedule</button> : null}>
      {error ? <div className="banner error" role="alert">{error}</div> : null}
      {editing && editing.report === null ? <ScheduleForm report={null} onDone={done} /> : null}
      {rows.length === 0 ? <span className="muted">No scheduled reports.</span> : null}
      {rows.map((r) => (
        <div key={r.id} className="cell-stack" style={{ borderTop: "1px solid var(--border)", paddingTop: 10 }}>
          <div className="row" style={{ justifyContent: "space-between" }}>
            <span style={{ fontWeight: 600 }}>{r.name}</span>
            <Chip tone={r.enabled ? "ok" : "off"}>{r.enabled ? r.kind : "Off"}</Chip>
          </div>
          <span className="small muted mono">{r.schedule}</span>
          <span className="small faint">{r.recipients.join(", ")}</span>
          <span className="small faint">Next {clock(r.next_run_at)} · last {clock(r.last_run_at)}</span>
          {canEdit ? (
            <div className="row" style={{ gap: 6 }}>
              <button className="btn" type="button" onClick={() => setEditing({ report: r })} aria-label={`Edit schedule ${r.name}`}>Edit</button>
              <button className="btn" type="button" onClick={() => act(postJson(`/api/v1/scheduled-reports/${r.id}/toggle`, {}))}>{r.enabled ? "Turn off" : "Turn on"}</button>
              {asking === r.id ? (
                <>
                  <button className="btn" type="button" style={{ borderColor: "var(--crit)", color: "var(--crit)" }} onClick={() => act(getJson(`/api/v1/scheduled-reports/${r.id}`, { method: "DELETE" }))}>Yes, remove</button>
                  <button className="btn" type="button" onClick={() => setAsking(null)}>Keep</button>
                </>
              ) : (
                <button className="btn" type="button" onClick={() => setAsking(r.id)}>Remove</button>
              )}
            </div>
          ) : null}
          {editing?.report?.id === r.id ? <ScheduleForm report={r} onDone={done} /> : null}
        </div>
      ))}
    </Card>
  );
}
