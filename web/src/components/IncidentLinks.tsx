import { useState, type FormEvent } from "react";
import { Link } from "react-router-dom";
import { postJson, type IncidentBrief } from "../lib/api";
import { shortNumber } from "../lib/format";
import { Card, Chip } from "./ui";

function Brief({ row }: { row: IncidentBrief }) {
  return (
    <Link to={`/incidents/${encodeURIComponent(row.number)}`} className="row" style={{ gap: 8, color: "var(--text)" }}>
      <span className="mono faint">{shortNumber(row.number)}</span><span>{row.title}</span>
      <Chip tone={row.status === "RESOLVED" || row.status === "CLOSED" ? "off" : "warn"}>{row.status.toLowerCase()}</Chip>
    </Link>
  );
}

/**
 * "Caused by" and "consequences" set by a person when the dependency map did not catch it, and a
 * one-click maintenance window for this incident's host (alarms muted, with an end and a reason).
 */
export function IncidentLinks({ number, links, canWork, canQuiet, hasAsset, onChanged }: {
  number: string; links: { caused_by: IncidentBrief | null; consequences: IncidentBrief[] } | undefined;
  canWork: boolean; canQuiet: boolean; hasAsset: boolean; onChanged: () => void;
}) {
  const [cause, setCause] = useState("");
  const [hours, setHours] = useState(2);
  const [reason, setReason] = useState("");
  const [msg, setMsg] = useState("");
  const [error, setError] = useState("");
  const call = (url: string, body: unknown, ok: (r: Record<string, string>) => string) => {
    setError("");
    postJson<Record<string, string>>(url, body).then((r) => (setMsg(ok(r)), onChanged())).catch((e: Error) => setError(e.message));
  };
  const link = (e: FormEvent) => (e.preventDefault(), call(`/api/v1/incidents/${encodeURIComponent(number)}/caused-by`, { caused_by: cause.trim() }, () => (setCause(""), "Link saved.")));
  const quiet = (e: FormEvent) => (e.preventDefault(), call(`/api/v1/incidents/${encodeURIComponent(number)}/quiet-host`, { hours, reason },
    (r) => `${r.asset} is in maintenance until ${new Date(r.until).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" })}.`));
  const effects = links?.consequences ?? [];
  return (
    <Card title="Related and quiet">
      {links?.caused_by ? (
        <div className="cell-stack" style={{ gap: 4 }}>
          <span className="small faint">Caused by</span>
          <Brief row={links.caused_by} />
          {canWork ? <button className="btn" type="button" style={{ alignSelf: "flex-start" }} onClick={() => call(`/api/v1/incidents/${encodeURIComponent(number)}/caused-by`, { caused_by: "" }, () => "Link removed.")}>Remove link</button> : null}
        </div>
      ) : canWork ? (
        <form className="row" style={{ gap: 8, flexWrap: "wrap" }} onSubmit={link}>
          <input className="search" aria-label="Caused by incident" placeholder="Caused by INC-…" value={cause} onChange={(e) => setCause(e.target.value)} style={{ flex: "1 1 180px", minWidth: 0 }} />
          <button className="btn" type="submit" disabled={!cause.trim()}>Link</button>
        </form>
      ) : <span className="small faint">Not linked to another incident.</span>}
      {effects.length ? (
        <div className="cell-stack" style={{ gap: 4 }}>
          <span className="small faint">Consequences · {effects.length}</span>
          {effects.slice(0, 10).map((r) => <Brief key={r.number} row={r} />)}
        </div>
      ) : null}
      {canQuiet && hasAsset ? (
        <form className="row" style={{ gap: 8, flexWrap: "wrap", borderTop: "1px solid var(--border)", paddingTop: 10 }} onSubmit={quiet}>
          <span className="small">Quiet this host for</span>
          <select className="search" aria-label="Hours of maintenance" value={hours} onChange={(e) => setHours(Number(e.target.value))} style={{ minWidth: 0, width: "auto" }}>
            {[1, 2, 4, 8, 24].map((h) => <option key={h} value={h}>{h} h</option>)}
          </select>
          <input className="search" aria-label="Why" placeholder="Why (e.g. vendor replacing PSU)" value={reason} onChange={(e) => setReason(e.target.value)} style={{ flex: "1 1 180px", minWidth: 0 }} />
          <button className="btn" type="submit" disabled={!reason.trim()}>Start maintenance</button>
        </form>
      ) : null}
      {msg ? <span className="small" role="status" style={{ color: "var(--ok)" }}>{msg}</span> : null}
      {error ? <span className="small" role="alert" style={{ color: "var(--crit)" }}>{error}</span> : null}
    </Card>
  );
}
