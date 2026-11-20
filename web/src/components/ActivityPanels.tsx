import { useEffect, useState } from "react";
import { Link } from "react-router-dom";
import { ApiError, getJson } from "../lib/api";
import { clock } from "../lib/format";
import { Card, Chip } from "./ui";

const PAGE = 50;
type Outbox = { total: number; rows: { id: number; at: string | null; channel: string; target: string; subject: string; status: string; error: string; step: string; incident: string }[] };
type Journal = { modules: { module: string; count: number }[]; entries: { id: number; at: string | null; module: string; action: string; status: string; summary: string; detail: string }[] };
type Audit = { total: number; rows: { id: number; at: string | null; actor: string; action: string; object: string; ip: string; data: Record<string, unknown> }[] };

function Pager({ total, offset, setOffset }: { total: number; offset: number; setOffset: (n: number) => void }) {
  if (total <= PAGE) return null;
  return (
    <div className="row small">
      <button className="btn" type="button" disabled={offset === 0} onClick={() => setOffset(Math.max(0, offset - PAGE))}>Previous</button>
      <span className="faint">{offset + 1}–{Math.min(total, offset + PAGE)} of {total}</span>
      <button className="btn" type="button" disabled={offset + PAGE >= total} onClick={() => setOffset(offset + PAGE)}>Next</button>
    </div>
  );
}

const sentTone = (s: string) => (s === "sent" ? "ok" : s === "queued" || s === "generated" ? "off" : "crit");

/** Every mail and chat message, with whether it was delivered: the answer to "did the alarm go out?". */
export function OutboxCard() {
  const [status, setStatus] = useState("");
  const [q, setQ] = useState("");
  const [offset, setOffset] = useState(0);
  const [data, setData] = useState<Outbox | null>(null);
  const [error, setError] = useState("");
  const [hidden, setHidden] = useState(false);
  useEffect(() => {
    const params = new URLSearchParams({ status, q, limit: String(PAGE), offset: String(offset) });
    getJson<Outbox>(`/api/v1/outbox?${params}`).then(setData).catch((e: Error) => (e instanceof ApiError && e.status === 403 ? setHidden(true) : setError(e.message)));
  }, [status, q, offset]);
  if (hidden) return null;
  return (
    <Card title="Mail and chat outbox" actions={<span className="small faint">{data ? `${data.total} messages` : ""}</span>}>
      <div className="row">
        {[["", "All"], ["failed", "Not delivered"], ["sent", "Sent"]].map(([v, l]) => <button key={v} className="btn" aria-pressed={status === v} onClick={() => (setStatus(v), setOffset(0))}>{l}</button>)}
        <input className="search" type="search" aria-label="Search the outbox" placeholder="Recipient, subject, INC-…" value={q} onChange={(e) => (setQ(e.target.value), setOffset(0))} />
      </div>
      {error ? <span className="small" style={{ color: "var(--crit)" }}>{error}</span> : null}
      <table className="data">
        <thead><tr><th style={{ width: 90 }}>When</th><th style={{ width: 80 }}>Channel</th><th>To</th><th>About</th><th style={{ width: 110 }}>Status</th></tr></thead>
        <tbody>
          {(data?.rows ?? []).map((r) => (
            <tr key={r.id}>
              <td className="small muted">{clock(r.at)}</td>
              <td className="small">{r.channel}</td>
              <td className="small" style={{ wordBreak: "break-word" }}>{r.target}</td>
              <td className="small">
                {r.incident ? <Link className="mono" to={`/incidents/${encodeURIComponent(r.incident)}`}>{r.incident.split("_")[0]}</Link> : null} {r.subject}
                {r.error ? <div className="small" style={{ color: "var(--crit)" }}>{r.error}</div> : null}
              </td>
              <td><Chip tone={sentTone(r.status)}>{r.status}</Chip></td>
            </tr>
          ))}
        </tbody>
      </table>
      {data && !(data.rows ?? []).length ? <span className="muted">Nothing here.</span> : null}
      <Pager total={data?.total ?? 0} offset={offset} setOffset={setOffset} />
      <span className="small faint">“generated” means SMTP is off, so the mail was written but not sent. Same list: <code className="mono">./forgesre journal notification</code>.</span>
    </Card>
  );
}

/** What each module did and where it failed: syncs, jobs, notifications, discovery, backups. */
export function JournalPanel() {
  const [module, setModule] = useState("");
  const [status, setStatus] = useState("");
  const [data, setData] = useState<Journal | null>(null);
  const [error, setError] = useState("");
  useEffect(() => {
    const params = new URLSearchParams({ limit: "200", ...(module ? { module } : {}), ...(status ? { status } : {}) });
    getJson<Journal>(`/api/v1/journal?${params}`).then(setData).catch((e: Error) => setError(e.message));
  }, [module, status]);
  return (
    <Card title="Journal">
      <span className="small faint">The platform&apos;s own report: every sync, job, delivery and check with its result. The first place to look when an integration misbehaves. Same as <code className="mono">./forgesre journal</code>.</span>
      <div className="row">
        <select className="search" aria-label="Module" value={module} onChange={(e) => setModule(e.target.value)}>
          <option value="">All modules</option>
          {(data?.modules ?? []).map((m) => <option key={m.module} value={m.module}>{m.module} ({m.count})</option>)}
        </select>
        {[["", "All"], ["error", "Errors"]].map(([v, l]) => <button key={v} className="btn" aria-pressed={status === v} onClick={() => setStatus(v)}>{l}</button>)}
      </div>
      {error ? <span className="small" style={{ color: "var(--crit)" }}>{error}</span> : null}
      <table className="data">
        <thead><tr><th style={{ width: 90 }}>When</th><th style={{ width: 150 }}>Module</th><th>What happened</th><th style={{ width: 80 }}>Result</th></tr></thead>
        <tbody>
          {(data?.entries ?? []).map((e) => (
            <tr key={e.id}>
              <td className="small muted">{clock(e.at)}</td>
              <td className="small mono">{e.module}.{e.action}</td>
              <td className="small">{e.summary}{e.detail && e.status === "error" ? <div className="small faint" style={{ wordBreak: "break-word" }}>{e.detail.slice(0, 300)}</div> : null}</td>
              <td><Chip tone={e.status === "ok" ? "ok" : e.status === "error" ? "crit" : "warn"}>{e.status}</Chip></td>
            </tr>
          ))}
        </tbody>
      </table>
    </Card>
  );
}

/** Who changed what (Admin). */
export function AuditPanel() {
  const [q, setQ] = useState("");
  const [offset, setOffset] = useState(0);
  const [data, setData] = useState<Audit | null>(null);
  const [error, setError] = useState("");
  useEffect(() => {
    getJson<Audit>(`/api/v1/audit?${new URLSearchParams({ q, limit: String(PAGE), offset: String(offset) })}`).then(setData).catch((e: Error) => setError(e.message));
  }, [q, offset]);
  return (
    <Card title="Audit log" actions={<span className="small faint">{data ? `${data.total} changes` : ""}</span>}>
      <span className="small faint">Every change to rules, playbooks, escalation, noise filters, settings, users, assets and incidents, with who made it.</span>
      <input className="search" type="search" aria-label="Search the audit log" placeholder="Person, action, object…" value={q} onChange={(e) => (setQ(e.target.value), setOffset(0))} />
      {error ? <span className="small" style={{ color: "var(--crit)" }}>{error}</span> : null}
      <table className="data">
        <thead><tr><th style={{ width: 90 }}>When</th><th style={{ width: 170 }}>Who</th><th style={{ width: 170 }}>Action</th><th>On</th></tr></thead>
        <tbody>
          {(data?.rows ?? []).map((r) => (
            <tr key={r.id}>
              <td className="small muted">{clock(r.at)}</td>
              <td className="small" style={{ wordBreak: "break-word" }}>{r.actor}</td>
              <td className="small mono">{r.action}</td>
              <td className="small" style={{ wordBreak: "break-word" }}>{r.object}{Object.keys(r.data).length ? <div className="faint">{JSON.stringify(r.data).slice(0, 160)}</div> : null}</td>
            </tr>
          ))}
        </tbody>
      </table>
      <Pager total={data?.total ?? 0} offset={offset} setOffset={setOffset} />
    </Card>
  );
}
