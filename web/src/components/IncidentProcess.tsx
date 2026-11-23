import { useEffect, useState } from "react";
import { getJson, postJson, type FollowUp, type MajorView, type SlaClock, type SlaState } from "../lib/api";
import { clock } from "../lib/format";
import { Card, Chip } from "./ui";

const mins = (m: number) => (Math.abs(m) < 60 ? `${Math.abs(m)} min` : `${Math.floor(Math.abs(m) / 60)} h ${Math.abs(m) % 60} min`);

/** One SLA clock as a chip: met, breached, or how long is left. */
export function SlaChip({ label, c }: { label: string; c: SlaClock }) {
  if (c.state === "none") return null;
  if (c.state === "met") return <Chip tone="ok">{label} met</Chip>;
  if (c.state === "breached") return <Chip tone="crit">{label} breached</Chip>;
  const left = c.minutes_left ?? 0;
  return <Chip tone={left <= Math.max(5, (c.target_min ?? 60) / 5) ? "warn" : "off"}>{label} due in {mins(left)}</Chip>;
}

export function SlaChips({ sla }: { sla: SlaState | null | undefined }) {
  if (!sla) return null;
  return <span className="row" style={{ gap: 6 }} aria-label="SLA"><SlaChip label="Response" c={sla.response} /><SlaChip label="Resolve" c={sla.resolve} /></span>;
}

/**
 * Major incident: declare with a commander, a bridge link and an update interval; then post status
 * updates (saved as notes and sent to the chat channels) and copy the stakeholder message.
 */
export function MajorIncidentCard({ number, major, canWork, onChanged }: { number: string; major: MajorView; canWork: boolean; onChanged: () => void }) {
  const [form, setForm] = useState({ commander: major.commander, bridge_url: major.bridge_url, update_minutes: major.update_minutes || 30 });
  const [declaring, setDeclaring] = useState(false);
  const [update, setUpdate] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [copied, setCopied] = useState(false);
  const url = `/api/v1/incidents/${encodeURIComponent(number)}`;
  const run = async (fn: () => Promise<unknown>) => {
    setBusy(true);
    setError("");
    try {
      await fn();
      onChanged();
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setBusy(false);
    }
  };
  if (!major.on) {
    if (!canWork) return null;
    return declaring ? (
      <Card title="Declare a major incident">
        <div className="form-grid">
          <label>Incident commander<input value={form.commander} placeholder="you" onChange={(e) => setForm({ ...form, commander: e.target.value })} /></label>
          <label>Bridge call link<input value={form.bridge_url} placeholder="https://teams.microsoft.com/… or tel:+381…" onChange={(e) => setForm({ ...form, bridge_url: e.target.value })} /></label>
          <label>Status update every (min)<input type="number" min={5} max={240} value={form.update_minutes} onChange={(e) => setForm({ ...form, update_minutes: Number(e.target.value) })} /></label>
        </div>
        <div className="row" style={{ gap: 8 }}>
          <button className="btn primary" type="button" disabled={busy} onClick={() => void run(() => postJson(`${url}/major`, { on: true, ...form }))}>Declare</button>
          <button className="btn" type="button" onClick={() => setDeclaring(false)}>Cancel</button>
        </div>
        {error ? <span className="small" role="alert" style={{ color: "var(--crit)" }}>{error}</span> : null}
      </Card>
    ) : (
      <button className="btn" type="button" style={{ alignSelf: "flex-start" }} onClick={() => setDeclaring(true)}>Declare major incident</button>
    );
  }
  const dueIn = major.next_due ? Math.round((Date.parse(major.next_due) - Date.now()) / 60000) : null;
  return (
    <Card title="Major incident" actions={<Chip tone={major.overdue ? "crit" : "warn"}>{major.overdue ? `update overdue ${mins(dueIn ?? 0)}` : `next update in ${mins(dueIn ?? 0)}`}</Chip>}>
      <div className="row" style={{ gap: 16, flexWrap: "wrap" }}>
        <span>Commander: <strong>{major.commander}</strong></span>
        {major.bridge_url ? <a href={major.bridge_url} target="_blank" rel="noopener noreferrer">Join the bridge call</a> : <span className="faint">No bridge link</span>}
        <span className="small faint">every {major.update_minutes} min{major.last_update_at ? ` · last update ${clock(major.last_update_at)}` : ""}</span>
      </div>
      {canWork ? (
        <div className="cell-stack" style={{ gap: 6 }}>
          <textarea aria-label="Status update" rows={2} value={update} maxLength={2000} placeholder="What we know, what we are doing, what is next"
            onChange={(e) => setUpdate(e.target.value)} style={{ width: "100%", font: "inherit" }} />
          <div className="row" style={{ gap: 8 }}>
            <button className="btn primary" type="button" disabled={busy || !update.trim()}
              onClick={() => void run(async () => (await postJson(`${url}/updates`, { text: update }), setUpdate("")))}>Post status update</button>
            <button className="btn" type="button" disabled={busy} onClick={() => void run(() => postJson(`${url}/major`, { on: false }))}>Stand down</button>
          </div>
        </div>
      ) : null}
      <details>
        <summary className="small" style={{ cursor: "pointer" }}>Message for management and other teams</summary>
        <pre className="small" style={{ whiteSpace: "pre-wrap", margin: "6px 0" }}>{major.message}</pre>
        <button className="btn" type="button" onClick={() => void navigator.clipboard?.writeText(major.message).then(() => setCopied(true)).catch(() => undefined)}>
          {copied ? "Copied" : "Copy message"}
        </button>
      </details>
      {error ? <span className="small" role="alert" style={{ color: "var(--crit)" }}>{error}</span> : null}
    </Card>
  );
}

/** Follow-up actions of one incident: what, who, by when; tick off when done. */
export function FollowUpActions({ number, actions, canWork, onChanged }: { number: string; actions: FollowUp[]; canWork: boolean; onChanged: () => void }) {
  const [draft, setDraft] = useState({ text: "", owner: "", due: "" });
  const [error, setError] = useState("");
  const save = (fn: () => Promise<unknown>) => fn().then(onChanged).catch((e: Error) => setError(e.message));
  return (
    <Card title="Follow-up actions" actions={<span className="small faint">{actions.filter((a) => a.done).length} / {actions.length} done</span>}>
      {actions.length ? (
        <ul className="cell-stack" style={{ listStyle: "none", padding: 0, margin: 0, gap: 6 }}>
          {actions.map((a) => (
            <li key={a.id} className="row" style={{ gap: 8, alignItems: "flex-start" }}>
              <input type="checkbox" aria-label={`Done: ${a.text}`} checked={a.done} disabled={!canWork}
                onChange={(e) => void save(() => getJson(`/api/v1/actions/${a.id}`, { method: "PATCH", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ done: e.target.checked }) }))} />
              <span style={{ flex: 1, textDecoration: a.done ? "line-through" : undefined, opacity: a.done ? 0.6 : 1 }}>{a.text}</span>
              <span className="small faint">{a.owner || "no owner"}</span>
              {a.due ? <span className="small" style={{ color: a.overdue ? "var(--crit)" : undefined }}>{a.overdue ? "overdue " : "due "}{a.due}</span> : null}
            </li>
          ))}
        </ul>
      ) : <span className="small faint">No follow-up actions yet.</span>}
      {canWork ? (
        <form className="row" style={{ gap: 6, flexWrap: "wrap" }} onSubmit={(e) => (e.preventDefault(), void save(async () => (await postJson(`/api/v1/incidents/${encodeURIComponent(number)}/actions`, draft), setDraft({ text: "", owner: "", due: "" }))))}>
          <input aria-label="New action" placeholder="What must change" value={draft.text} onChange={(e) => setDraft({ ...draft, text: e.target.value })} style={{ flex: "2 1 220px" }} />
          <input aria-label="Owner" placeholder="Owner" value={draft.owner} onChange={(e) => setDraft({ ...draft, owner: e.target.value })} style={{ flex: "1 1 120px" }} />
          <input aria-label="Due" type="date" value={draft.due} onChange={(e) => setDraft({ ...draft, due: e.target.value })} />
          <button className="btn" type="submit" disabled={!draft.text.trim()}>Add</button>
        </form>
      ) : null}
      {error ? <span className="small" role="alert" style={{ color: "var(--crit)" }}>{error}</span> : null}
    </Card>
  );
}

type SlaReport = { days: number; enabled: boolean; by_severity: Record<string, { response_met: number; response_breached: number; resolve_met: number; resolve_breached: number }>; targets: { response: Record<string, number>; resolve: Record<string, number> } };

const pct = (met: number, breached: number) => (met + breached ? `${Math.round((100 * met) / (met + breached))}%` : "—");

/** Reports: SLA met per severity (acknowledged in time, resolved in time). */
export function SlaCard({ days }: { days: number }) {
  const [data, setData] = useState<SlaReport | null>(null);
  useEffect(() => void getJson<SlaReport>(`/api/v1/sla?days=${days}`).then((d) => setData(d?.by_severity ? d : null)).catch(() => setData(null)), [days]);
  if (!data) return null;
  if (!data.enabled) return null;
  return (
    <Card title="SLA" actions={<span className="small faint">Targets: Settings › SLA</span>}>
      <div className="table-wrap"><table className="data">
        <thead><tr><th>Severity</th><th>Acknowledged in time</th><th>Resolved in time</th><th>Targets (min)</th></tr></thead>
        <tbody>
          {Object.entries(data.by_severity).map(([sev, r]) => (
            <tr key={sev}>
              <td>{sev}</td>
              <td>{pct(r.response_met, r.response_breached)} <span className="small faint">({r.response_breached} breached)</span></td>
              <td>{pct(r.resolve_met, r.resolve_breached)} <span className="small faint">({r.resolve_breached} breached)</span></td>
              <td className="small">ack {data.targets.response[sev]} · resolve {data.targets.resolve[sev]}</td>
            </tr>
          ))}
        </tbody>
      </table></div>
    </Card>
  );
}

/** Reports: follow-up actions not done yet, overdue first. */
export function OpenActionsCard() {
  const [rows, setRows] = useState<FollowUp[] | null>(null);
  useEffect(() => void getJson<{ actions: FollowUp[] }>("/api/v1/actions").then((d) => setRows(Array.isArray(d?.actions) ? d.actions : [])).catch(() => setRows([])), []);
  if (!rows) return null;
  return (
    <Card title="Open follow-up actions" actions={<span className="small faint">{rows.filter((r) => r.overdue).length} overdue</span>}>
      {rows.length ? (
        <ul className="cell-stack" style={{ listStyle: "none", padding: 0, margin: 0, gap: 4 }}>
          {rows.slice(0, 10).map((a) => (
            <li key={a.id} className="row" style={{ gap: 8 }}>
              <a className="mono small" href={`/app/incidents/${encodeURIComponent(a.incident ?? "")}`}>{(a.incident ?? "").split("_")[0]}</a>
              <span style={{ flex: 1 }}>{a.text}</span>
              <span className="small faint">{a.owner || "no owner"}</span>
              <span className="small" style={{ color: a.overdue ? "var(--crit)" : undefined }}>{a.due ?? "no date"}</span>
            </li>
          ))}
        </ul>
      ) : <span className="small faint">Nothing open.</span>}
    </Card>
  );
}
