import { Link } from "react-router-dom";
import type { NocIncident } from "../lib/api";
import { FORGEAI_LABEL, clock, duration, severityLabel, severityTone, shortNumber, tierLabel, tierRole } from "../lib/format";
import { Chip, Dot } from "./ui";

// Incident table shared by the NOC overview and the Incidents page (design: two-line cells).
function IncidentRow({ row }: { row: NocIncident }) {
  const tone = severityTone(row.severity);
  const ai = FORGEAI_LABEL[row.forgeai] ?? FORGEAI_LABEL.none;
  return (
    <tr>
      <td>
        <span className="row" style={{ gap: 8 }}>
          <Dot tone={tone} />
          <span style={{ color: `var(--${tone === "info" ? "accent" : tone})`, fontWeight: 600, fontSize: 12.5 }}>{severityLabel(row.severity)}</span>
        </span>
      </td>
      <td>
        <div className="cell-stack">
          <span className="mono">{clock(row.started_at)}</span>
          <span className="small muted">{duration(row.started_at, row.resolved_at ? new Date(row.resolved_at) : new Date())}</span>
        </div>
      </td>
      <td>
        <div className="cell-stack">
          <span style={{ fontWeight: 600, overflowWrap: "anywhere" }}>{row.asset?.hostname ?? "—"}</span>
          <span className="small muted">{[row.asset?.type, row.source].filter(Boolean).join(" · ")}</span>
        </div>
      </td>
      <td>
        <div className="cell-stack">
          <Link to={`/incidents/${encodeURIComponent(row.number)}`} style={{ color: "var(--text)", fontWeight: 600 }}>
            {row.title} <span className="mono faint" title={row.number} style={{ whiteSpace: "nowrap" }}>{shortNumber(row.number)}</span>
          </Link>
          {row.detail ? (
            <span className="small muted" title={row.detail} style={{ display: "-webkit-box", WebkitLineClamp: 2, WebkitBoxOrient: "vertical", overflow: "hidden" }}>{row.detail}</span>
          ) : null}
        </div>
      </td>
      <td>
        {row.ack_at ? (
          <div className="cell-stack" style={{ minWidth: 0, maxWidth: 110 }}>
            <span className="small" style={{ color: "var(--ok)", fontWeight: 600 }}>Acknowledged</span>
            <span className="small muted" title={row.ack_by} style={{ display: "block", maxWidth: "100%", overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>{(row.ack_by || "").split("@")[0]}</span>
          </div>
        ) : row.resolved_at ? (
          <span className="small muted">—</span>
        ) : (
          <span className="small" style={{ color: `var(--${tone === "crit" ? "crit" : "high"})`, fontWeight: 600 }}>Not acked</span>
        )}
      </td>
      <td>
        <div className="cell-stack" title={tierRole(row.level)}>
          <span className="small" style={{ fontWeight: 600 }}>{tierLabel(row.level) || "—"}</span>
          <span className="small muted">{row.messages} message{row.messages === 1 ? "" : "s"}</span>
        </div>
      </td>
      <td className="small muted">
        <span title={row.forgerca} style={{ display: "-webkit-box", WebkitLineClamp: 2, WebkitBoxOrient: "vertical", overflow: "hidden" }}>{row.forgerca || "—"}</span>
      </td>
      <td><Chip tone={ai.tone}>{ai.text}</Chip></td>
    </tr>
  );
}

export function IncidentTable({ rows, loaded }: { rows: NocIncident[]; loaded: boolean }) {
  return (
    <div className="table-wrap">
      <table className="data" style={{ minWidth: 1040 }}>
        <thead>
          <tr>
            <th style={{ width: 100 }}>Severity</th>
            <th style={{ width: 90 }}>Started</th>
            <th style={{ width: 170 }} title="Asset, its type and where the alarm came from">Host</th>
            <th>Problem</th>
            <th style={{ width: 110 }}>Acknowledged</th>
            <th style={{ width: 100 }} title="Escalation step reached and messages sent">Escalation</th>
            <th style={{ width: 170 }}>ForgeRCA</th>
            <th style={{ width: 110 }}>ForgeAI</th>
          </tr>
        </thead>
        <tbody>
          {rows.map((row) => (
            <IncidentRow key={row.number} row={row} />
          ))}
        </tbody>
      </table>
      {loaded && rows.length === 0 ? <div className="empty">Nothing here.</div> : null}
    </div>
  );
}
