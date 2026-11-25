import { sevClass } from "../lib/sevcells";
import { Fragment, useEffect, useState } from "react";
import { Link } from "react-router-dom";
import { getJson, postJson, type NocIncident } from "../lib/api";
import { FORGEAI_LABEL, clock, duration, severityLabel, severityTone, shortNumber, tierLabel } from "../lib/format";
import { Chip, Dot } from "./ui";

// Incident table shared by the NOC overview and the Incidents page (design: two-line cells).
type Brief = { forgeai: { text: string; cause: string; action: string; model: string; at: string | null }; rca: { cause: string; actions: string[] } | null };

/**
 * ForgeAI's opinion under an incident row, opened from the ForgeAI cell. A grouped incident shows
 * the analysis of the one it belongs to, with a button to analyse this one on its own.
 */
function ForgeAiBrief({ row, cols, canWork, onChanged }: { row: NocIncident; cols: number; canWork: boolean; onChanged?: () => void }) {
  const target = row.forgeai === "grouped" && row.forgeai_ref_number ? row.forgeai_ref_number : row.number;
  const [brief, setBrief] = useState<Brief | null>(null);
  const [error, setError] = useState("");
  const [asked, setAsked] = useState(false);
  useEffect(() => {
    getJson<Brief>(`/api/v1/incidents/${encodeURIComponent(target)}`).then(setBrief).catch((e: Error) => setError(e.message));
  }, [target, row.forgeai]);
  const analyse = () => postJson(`/api/v1/incidents/${encodeURIComponent(row.number)}/forgeai`, { task: "incident", skill: "" })
    .then(() => (setAsked(true), onChanged?.())).catch((e: Error) => setError(e.message));
  const ai = brief?.forgeai;
  const done = Boolean(ai && (ai.cause || ai.text));
  return (
    <tr className="ai-brief-row">
      <td colSpan={cols}>
        <div className="ai-brief" role="region" aria-label={`ForgeAI on ${shortNumber(row.number)}`}>
          {row.forgeai === "grouped" ? <span className="small muted">Grouped with <Link to={`/incidents/${encodeURIComponent(target)}`}>{row.forgeai_ref}</Link> (same alarm or caused by it): its analysis covers this one.</span> : null}
          {error ? <span className="small" role="alert" style={{ color: "var(--crit)" }}>{error}</span> : null}
          {!brief && !error ? <span className="muted small">Loading…</span> : null}
          {done ? (
            <>
              {ai!.cause ? <div><strong>Likely cause</strong><p>{ai!.cause}</p></div> : null}
              {ai!.action ? <div><strong>First checks</strong><p style={{ whiteSpace: "pre-wrap" }}>{ai!.action}</p></div> : null}
              {ai!.text && ai!.text !== ai!.cause ? <div><strong>Summary</strong><p>{ai!.text}</p></div> : null}
              <span className="small faint">{ai!.model}{ai!.at ? ` · ${clock(ai!.at)}` : ""} · advisory, check it against the facts</span>
            </>
          ) : brief ? (
            <span className="small muted">
              {row.forgeai === "running" ? "ForgeAI is reading the metrics, logs and history of this incident now." :
                row.forgeai === "queued" ? `Waiting for the model: ${row.forgeai_queue ? `number ${row.forgeai_queue} in the queue (critical first)` : "in the queue"}.` :
                row.forgeai === "failed" ? "The last ForgeAI pass failed (model not answering or timed out)." :
                "No ForgeAI analysis yet."}
              {brief.rca?.cause ? <> ForgeRCA meanwhile: <strong>{brief.rca.cause}</strong></> : null}
            </span>
          ) : null}
          <div className="row" style={{ gap: 8 }}>
            <Link className="btn" to={`/incidents/${encodeURIComponent(row.number)}`}>Open incident</Link>
            {canWork && ["none", "failed", "grouped"].includes(row.forgeai) && !asked ? (
              <button className="btn" type="button" onClick={() => void analyse()}>{row.forgeai === "grouped" ? "Analyse this one on its own" : row.forgeai === "failed" ? "Try again" : "Analyse now"}</button>
            ) : null}
            {asked ? <span className="small muted" role="status">Queued.</span> : null}
          </div>
        </div>
      </td>
    </tr>
  );
}

function IncidentRow({ row, picked, onPick, open, onToggle }: { row: NocIncident; picked?: boolean; onPick?: (on: boolean) => void; open: boolean; onToggle: () => void }) {
  const tone = severityTone(row.severity);
  const ai = FORGEAI_LABEL[row.forgeai] ?? FORGEAI_LABEL.none;
  const hint = row.forgeai === "grouped" ? `with ${row.forgeai_ref}` : row.forgeai === "queued" && row.forgeai_queue ? `#${row.forgeai_queue} in queue` : ai.hint;
  return (
    <tr className={`${picked ? "picked" : ""}${open ? " ai-open" : ""}`.trim() || undefined}>
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
      <td>
        {row.forgeai === "off" ? <Chip tone="off">Off</Chip> : (
          <button type="button" className={`ai-cell${open ? " open" : ""}`} aria-expanded={open} onClick={onToggle}
            aria-label={`ForgeAI ${ai.text} for ${shortNumber(row.number)}: ${open ? "hide" : "show"} the analysis`}>
            <span className="row" style={{ gap: 6 }}><Dot tone={ai.tone} /><strong className="small">{ai.text}</strong><span aria-hidden="true">{open ? "▴" : "▾"}</span></span>
            {hint ? <span className="small faint">{hint}</span> : null}
          </button>
        )}
      </td>
    </tr>
  );
}

/**
 * Incident table. With ``canWork``, rows get checkboxes and a bar to acknowledge or resolve the
 * selected ones at once (POST /api/v1/incidents/bulk), e.g. twenty alarms from one cause.
 */
export function IncidentTable({ rows, loaded, canWork = false, onChanged }: { rows: NocIncident[]; loaded: boolean; canWork?: boolean; onChanged?: () => void }) {
  const [picked, setPicked] = useState<Set<string>>(new Set());
  const [openAi, setOpenAi] = useState<Set<string>>(new Set());
  const toggleAi = (number: string) => setOpenAi((s) => {
    const next = new Set(s);
    if (next.has(number)) next.delete(number); else next.add(number);
    return next;
  });
  const cols = canWork ? 11 : 10;
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
            <th style={{ width: 140 }}>ForgeAI</th>
          </tr>
        </thead>
        <tbody>
          {rows.map((row) => (
            <Fragment key={row.number}>
              <IncidentRow row={row} picked={picked.has(row.number)} onPick={canWork ? (on) => pick(row.number, on) : undefined}
                open={openAi.has(row.number)} onToggle={() => toggleAi(row.number)} />
              {openAi.has(row.number) ? <ForgeAiBrief row={row} cols={cols} canWork={canWork} onChanged={onChanged} /> : null}
            </Fragment>
          ))}
        </tbody>
      </table>
      {loaded && rows.length === 0 ? <div className="empty">Nothing here.</div> : null}
    </div>
    </>
  );
}
