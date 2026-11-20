import { Link } from "react-router-dom";
import type { NocIncident } from "../lib/api";
import { FORGEAI_LABEL, clock, duration, severityLabel, severityTone, shortNumber, tierLabel } from "../lib/format";
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
          <span className="small muted nowrap">{duration(row.started_at, row.resolved_at ? new Date(row.resolved_at) : new Date())}</span>
        </div>
      </td>
      <td>
        <div className="cell-stack">
          <span style={{ fontWeight: 600 }}>{row.asset?.hostname ?? "—"}</span>
          <span className="small muted">{row.asset?.type ?? ""}</span>
        </div>
      </td>
      <td>
        <div className="cell-stack">
          <Link to={`/incidents/${encodeURIComponent(row.number)}`} style={{ color: "var(--text)", fontWeight: 600 }}>
            {row.title} <span className="mono faint" title={row.number}>{shortNumber(row.number)}</span>
          </Link>
          {row.detail ? <span className="small muted">{row.detail}</span> : null}
        </div>
      </td>
      <td className="small muted">{row.source || "—"}</td>
      <td>
        {row.ack_at ? (
          <div className="cell-stack">
            <span className="small" style={{ color: "var(--ok)", fontWeight: 600 }}>Acked</span>
            <span className="small muted">{row.ack_by}</span>
          </div>
        ) : row.resolved_at ? (
          <span className="small muted">—</span>
        ) : (
          <span className="small" style={{ color: `var(--${tone === "crit" ? "crit" : "high"})`, fontWeight: 600 }}>Not acked</span>
        )}
      </td>
      <td><Chip tone="off">{tierLabel(row.level)}</Chip></td>
      <td className="small muted">{row.messages} sent</td>
      <td className="small muted">{row.forgerca || "—"}</td>
      <td><Chip tone={ai.tone}>{ai.text}</Chip></td>
    </tr>
  );
}

export function IncidentTable({ rows, loaded }: { rows: NocIncident[]; loaded: boolean }) {
  return (
    <div className="table-wrap">
      <table className="data" style={{ minWidth: 1180 }}>
        <thead>
          <tr>
            <th style={{ width: 100 }}>Severity</th>
            <th style={{ width: 90 }}>Started</th>
            <th style={{ width: 150 }}>Host</th>
            <th>Problem</th>
            <th style={{ width: 90 }}>Source</th>
            <th style={{ width: 96 }}>Ack</th>
            <th style={{ width: 80 }}>Tier</th>
            <th style={{ width: 80 }}>Messages</th>
            <th style={{ width: 170 }}>ForgeRCA</th>
            <th style={{ width: 128 }}>ForgeAI</th>
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
