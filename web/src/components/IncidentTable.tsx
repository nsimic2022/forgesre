import { sevClass } from "../lib/sevcells";
import { useState } from "react";
import { Link } from "react-router-dom";
import { postJson, type NocIncident } from "../lib/api";
import { FORGEAI_LABEL, clock, duration, severityLabel, severityTone, shortNumber, tierLabel } from "../lib/format";
import { Chip, Dot } from "./ui";

// Incident table shared by the NOC overview and the Incidents page (design: two-line cells).
function IncidentRow({ row, picked, onPick }: { row: NocIncident; picked?: boolean; onPick?: (on: boolean) => void }) {
  const tone = severityTone(row.severity);
  const ai = FORGEAI_LABEL[row.forgeai] ?? FORGEAI_LABEL.none;
  return (
    <tr className={picked ? "picked" : undefined}>
      {onPick ? <td><input type="checkbox" checked={Boolean(picked)} onChange={(e) => onPick(e.target.checked)} aria-label={`Select ${shortNumber(row.number)}`} /></td> : null}
      <td>
        <span className="row" style={{ gap: 8 }}>
          <Dot tone={tone} />
          <span style={{ color: `var(--${tone === "info" ? "accent" : tone})`, fontWeight: 600, fontSize: 12.5 }}>{severityLabel(row.severity)}</span>
        </span>
      </td>
      <td>
        <div className="cell-stack">
          <span className="mono">{clock(row.started_at)}</span>
          <span className="small muted nowrap">{duration(row.started_at, row.resolved_at ? new Date(row.resolved_at) : new Date())}</span>
        </div>
      </td>
      <td>
        <div className="cell-stack">
          <span style={{ fontWeight: 600 }}>{row.asset?.hostname ?? "—"}</span>
          <span className="small muted">{row.asset?.type ?? ""}</span>
        </div>
      </td>
      <td className={sevClass(row.severity)}>
        <div className="cell-stack">
          <Link to={`/incidents/${encodeURIComponent(row.number)}`} style={{ color: "var(--text)", fontWeight: 600 }}>
            {row.title} <span className="mono faint" title={row.number}>{shortNumber(row.number)}</span>
            {row.major ? <> <Chip tone="crit">major</Chip></> : null}
            {row.flap_count ? <> <Chip tone="warn">flapping ×{row.flap_count}</Chip></> : null}
            {row.sla && row.sla.state === "breached" ? <> <Chip tone="crit">{row.sla.kind === "response" ? "ack" : "resolve"} SLA breached</Chip></> : null}
            {row.caused_by ? <> <span className="small faint" title="Linked to the incident that caused it; this one does not page">↳ {row.caused_by}</span></> : null}
          </Link>
          {row.detail ? <span className="small muted">{row.detail}</span> : null}
        </div>
      </td>
      <td className="small muted">{row.source || "—"}</td>
      <td>
        {row.ack_at ? (
          // who and when are on the incident page and in the tooltip; the cell stays short so Tier fits
          <span className="small" style={{ color: "var(--ok)", fontWeight: 600 }} title={`Acknowledged by ${row.ack_by || "someone"}`}>Acknow</span>
        ) : row.resolved_at ? (
          <span className="small muted">—</span>
        ) : (
          <span className="small" style={{ color: `var(--${tone === "crit" ? "crit" : "high"})`, fontWeight: 600, whiteSpace: "nowrap" }}>Not acknow</span>
        )}
      </td>
      <td><Chip tone="off">{tierLabel(row.level)}</Chip></td>
      <td className="small muted">{row.messages} sent</td>
      <td className="small muted">{row.forgerca || "—"}</td>
      <td><Chip tone={ai.tone}>{ai.text}</Chip></td>
    </tr>
  );
}

/**
 * Incident table. With ``canWork``, rows get checkboxes and a bar to acknowledge or resolve the
 * selected ones at once (POST /api/v1/incidents/bulk), e.g. twenty alarms from one cause.
 */
export function IncidentTable({ rows, loaded, canWork = false, onChanged }: { rows: NocIncident[]; loaded: boolean; canWork?: boolean; onChanged?: () => void }) {
  const [picked, setPicked] = useState<Set<string>>(new Set());
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const visible = new Set(rows.map((r) => r.number));
  const chosen = [...picked].filter((n) => visible.has(n));
  const pick = (number: string, on: boolean) => setPicked((s) => {
    const next = new Set(s);
    if (on) next.add(number); else next.delete(number);
    return next;
  });
  const all = rows.length > 0 && rows.every((r) => picked.has(r.number));
  const act = (action: "ack" | "resolve") => {
    setBusy(true);
    setError("");
    postJson("/api/v1/incidents/bulk", { numbers: chosen, action })
      .then(() => (setPicked(new Set()), onChanged?.()))
      .catch((e: Error) => setError(e.message))
      .finally(() => setBusy(false));
  };
  return (
    <>
      {canWork && chosen.length ? (
        <div className="row bulk-bar" role="region" aria-label="Selected incidents">
          <strong>{chosen.length} selected</strong>
          <button className="btn primary" type="button" disabled={busy} onClick={() => act("ack")}>Acknowledge selected</button>
          <button className="btn" type="button" disabled={busy} onClick={() => act("resolve")}>Resolve selected</button>
          <button className="btn" type="button" onClick={() => setPicked(new Set())}>Clear</button>
          {error ? <span className="small" role="alert" style={{ color: "var(--crit)" }}>{error}</span> : null}
        </div>
      ) : null}
    <div className="table-wrap">
      <table className="data" style={{ minWidth: 1180 }}>
        <thead>
          <tr>
            {canWork ? <th style={{ width: 34 }}><input type="checkbox" aria-label="Select all on this page" checked={all} onChange={(e) => setPicked(e.target.checked ? new Set(rows.map((r) => r.number)) : new Set())} /></th> : null}
            <th style={{ width: 100 }}>Severity</th>
            <th style={{ width: 90 }}>Started</th>
            <th style={{ width: 150 }}>Host</th>
            <th>Problem</th>
            <th style={{ width: 90 }}>Source</th>
            <th style={{ width: 96 }} title="Acknowledged">Acknow.</th>
            <th style={{ width: 80 }}>Tier</th>
            <th style={{ width: 80 }}>Messages</th>
            <th style={{ width: 170 }}>ForgeRCA</th>
            <th style={{ width: 128 }}>ForgeAI</th>
          </tr>
        </thead>
        <tbody>
          {rows.map((row) => (
            <IncidentRow key={row.number} row={row} picked={picked.has(row.number)} onPick={canWork ? (on) => pick(row.number, on) : undefined} />
          ))}
        </tbody>
      </table>
      {loaded && rows.length === 0 ? <div className="empty">Nothing here.</div> : null}
    </div>
    </>
  );
}
