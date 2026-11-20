import { useState, type FormEvent } from "react";
import { getJson, postJson, putJson, type ScheduledReport } from "../lib/api";
import { clock } from "../lib/format";
import { MailFrame } from "./MessagePreviews";
import { Card, Chip } from "./ui";

type Preview = { subject: string; text: string; html: string; note: string; recipients: string[]; schedule: string };

const PRESETS: [string, string][] = [["15m", "Every 15 minutes"], ["60m", "Every hour"], ["6h", "Every 6 hours"], ["12h", "Every 12 hours"], ["24h", "Every day"], ["custom", "Custom interval"], ["cron", "Cron expression"], ["once", "Once"]];
const CRON_EXAMPLES: [string, string][] = [["0 7 * * *", "every day 07:00"], ["0 7 * * 1", "Mondays 07:00"], ["0 8 1 * *", "1st of the month 08:00"], ["30 6 * * 1-5", "weekdays 06:30"]];
const DAYS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"];

type Draft = { name: string; kind: string; recipients: string; schedule: string; every_n: string; every_unit: string; weekdays: number[]; cron: string };

export const KINDS: [string, string, string][] = [
  ["performance", "NOC summary", "Incidents of the period against the one before, what is open, frequent alerts, noisiest assets, then each asset's metrics."],
  ["forgeai", "ForgeAI digest", "The morning mail: incidents of the last day (up to a week), what is open and who has it, then ForgeAI's shift summary from those facts."],
];
const kindLabel = (k: string) => KINDS.find(([v]) => v === k)?.[1] ?? k;

function toDraft(r: ScheduledReport | null): Draft {
  const s = r?.spec;
  const minutes = s?.every_minutes ?? 0;
  return {
    name: r?.name ?? "Weekly NOC summary", kind: r?.kind ?? "performance", recipients: (r?.recipients ?? []).join(", "), schedule: s?.preset || (r ? "24h" : "cron"),
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
      <label className="cell-stack small faint">
        What
        <select className="search" style={{ minWidth: 0 }} value={d.kind} onChange={(e) => set({ kind: e.target.value, ...(report ? {} : e.target.value === "forgeai" ? { name: "Morning ForgeAI digest", schedule: "cron", cron: "0 7 * * *" } : { name: "Weekly NOC summary", cron: "0 7 * * 1" }) })}>
          {KINDS.map(([v, l]) => <option key={v} value={v}>{l}</option>)}
        </select>
        <span>{KINDS.find(([v]) => v === d.kind)?.[2]}</span>
      </label>
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
  const [preview, setPreview] = useState<{ id: number | null; data: Preview | null } | null>(null);
  const [sent, setSent] = useState<number | null>(null);
  const show = (id: number | null) => {
    setPreview({ id, data: null });
    getJson<Preview>(id === null ? "/api/v1/scheduled-reports/preview" : `/api/v1/scheduled-reports/${id}/preview`)
      .then((data) => setPreview({ id, data }))
      .catch((e: Error) => (setPreview(null), setError(e.message)));
  };
  const act = (call: Promise<unknown>) => call.then(() => (setAsking(null), onChanged())).catch((e: Error) => setError(e.message));
  const done = (saved: boolean) => (setEditing(null), saved && onChanged());
  return (
    <Card title="Scheduled reports" actions={canEdit ? <button className="btn primary" type="button" onClick={() => setEditing({ report: null })}>New schedule</button> : null}>
      <p className="small muted" style={{ margin: 0 }}>
        A scheduled report is mailed <strong>automatically</strong> to its recipients at each time it is due: no one has to press anything.
        It opens with the period at a glance (incidents against the period before, time to acknowledge and resolve), incidents per day,
        what is still open, the most frequent alerts and the noisiest assets. Then each asset (the ones picked for the report, or all of them) with its current metrics. It looks back one schedule interval: a day for a daily report, a week for a weekly one, a month for a monthly one.
        A <strong>ForgeAI digest</strong> is the morning mail instead: the incidents of the last day, what is open and who has it, then ForgeAI&apos;s shift summary written from those facts (left out, with the reason, when ForgeAI is off or its answer fails the checks).
        <strong> Preview</strong> shows the mail with today&apos;s data; <strong>Send now</strong> mails it once without moving the schedule.
      </p>
      <div className="row"><button className="btn" type="button" onClick={() => show(null)}>Preview a weekly report</button></div>
      {preview && preview.id === null ? <PreviewBox preview={preview.data} onClose={() => setPreview(null)} /> : null}
      {error ? <div className="banner error" role="alert">{error}</div> : null}
      {editing && editing.report === null ? <ScheduleForm report={null} onDone={done} /> : null}
      {rows.length === 0 ? <span className="muted">No scheduled reports.</span> : null}
      {rows.map((r) => (
        <div key={r.id} className="cell-stack" style={{ borderTop: "1px solid var(--border)", paddingTop: 10 }}>
          <div className="row" style={{ justifyContent: "space-between" }}>
            <span style={{ fontWeight: 600 }}>{r.name}</span>
            <Chip tone={r.enabled ? "ok" : "off"}>{r.enabled ? kindLabel(r.kind) : "Off"}</Chip>
          </div>
          <span className="small muted mono">{r.schedule}</span>
          <span className="small faint">{r.recipients.join(", ")}</span>
          <span className="small faint">Next {clock(r.next_run_at)} · last {clock(r.last_run_at)}</span>
          <div className="row" style={{ gap: 6 }}>
            <button className="btn" type="button" onClick={() => show(r.id)} aria-label={`Preview ${r.name}`}>Preview</button>
            {canEdit ? (
              <button className="btn" type="button" disabled={sent === r.id} onClick={() => act(postJson(`/api/v1/scheduled-reports/${r.id}/send`, {}).then(() => setSent(r.id)))}>
                {sent === r.id ? "Queued, sending…" : "Send now"}
              </button>
            ) : null}
          </div>
          {preview?.id === r.id ? <PreviewBox preview={preview.data} onClose={() => setPreview(null)} /> : null}
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

function PreviewBox({ preview, onClose }: { preview: Preview | null; onClose: () => void }) {
  return (
    <div className="cell-stack" style={{ gap: 8, borderTop: "1px solid var(--border)", paddingTop: 10 }}>
      <div className="row" style={{ justifyContent: "space-between" }}>
        <span className="small faint">{preview ? `${preview.schedule}${preview.recipients.length ? ` · to ${preview.recipients.join(", ")}` : ""}` : "Building the preview…"}</span>
        <button className="btn" type="button" onClick={onClose}>Close preview</button>
      </div>
      {preview?.note ? <span className="small muted">{preview.note}</span> : null}
      {preview && (preview.html || preview.text) ? <MailFrame mail={preview} title="Scheduled report preview" /> : null}
    </div>
  );
}
