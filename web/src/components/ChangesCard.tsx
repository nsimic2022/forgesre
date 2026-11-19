import type { ChangeRow } from "../lib/api";
import { Card, Chip } from "./ui";

function ago(minutes: number) {
  if (minutes < 1) return "just before";
  if (minutes < 60) return `${minutes} min before`;
  const h = Math.floor(minutes / 60);
  return `${h} h ${minutes % 60} min before`;
}

/** Incident page: deploys, config pushes, tickets and ForgeSRE edits in the 24 h before the alarm. */
export function ChangesCard({ changes }: { changes: ChangeRow[] }) {
  return (
    <Card title={`What changed before · ${changes.length}`} actions={<span className="small faint">24 h · this asset, its parent, global</span>}>
      {changes.length === 0 ? (
        <span className="small muted">No recorded change. Deploy tools can post to <code className="mono">POST /api/v1/changes</code>.</span>
      ) : (
        <ol style={{ margin: 0, padding: 0, display: "flex", flexDirection: "column", gap: 10 }}>
          {changes.map((c, i) => (
            <li key={`${c.at}-${i}`} style={{ listStyle: "none", borderTop: i ? "1px solid var(--border)" : 0, paddingTop: i ? 10 : 0 }} className="cell-stack">
              <span className="row" style={{ gap: 8 }}>
                <Chip tone={c.kind === "deploy" ? "info" : c.source === "forgesre" ? "off" : "warn"}>{c.kind}</Chip>
                <span className="small mono" style={{ color: c.before_min <= 60 ? "var(--high)" : "var(--faint)" }}>{ago(c.before_min)}</span>
              </span>
              {c.url ? <a href={c.url} rel="noreferrer" target="_blank" style={{ fontWeight: 600 }}>{c.title}</a> : <span style={{ fontWeight: 600 }}>{c.title}</span>}
              <span className="small faint">{[c.asset, c.source, c.actor].filter(Boolean).join(" · ")}</span>
              {c.detail ? <span className="small muted" style={{ whiteSpace: "pre-wrap" }}>{c.detail}</span> : null}
            </li>
          ))}
        </ol>
      )}
    </Card>
  );
}
