import { useState } from "react";
import { postJson, type Handover, type MajorBrief } from "../lib/api";
import { clock } from "../lib/format";
import { Card, Chip } from "./ui";

/** Red strip for every major incident in progress, with its commander and the update timer. */
export function MajorBanner({ majors }: { majors: MajorBrief[] | undefined }) {
  if (!majors?.length) return null;
  return (
    <>
      {majors.map((m) => (
        <div key={m.number} className="major-banner" role="alert">
          <strong>MAJOR INCIDENT</strong>
          <a href={`/app/incidents/${encodeURIComponent(m.number)}`}>{m.number.split("_")[0]} {m.title}</a>
          <span>commander {m.commander || "not named"}</span>
          {m.bridge_url ? <a href={m.bridge_url} target="_blank" rel="noopener noreferrer">bridge call</a> : null}
          {m.overdue ? <Chip tone="crit">status update overdue</Chip> : null}
        </div>
      ))}
    </>
  );
}

/**
 * Shift handover on the NOC page: the latest handover and whether the next shift took it over;
 * write a new one (notes + a snapshot of open incidents) or press "I take over".
 */
export function HandoverCard({ handover, canWork, me, onChanged }: { handover: Handover | null | undefined; canWork: boolean; me: string; onChanged: () => void }) {
  const [writing, setWriting] = useState(false);
  const [notes, setNotes] = useState("");
  const [error, setError] = useState("");
  const act = (fn: () => Promise<unknown>) => fn().then(() => (setWriting(false), setNotes(""), onChanged())).catch((e: Error) => setError(e.message));
  const pending = handover && !handover.taken_by;
  return (
    <Card title="Shift handover" actions={handover ? (pending ? <Chip tone="warn">not taken over</Chip> : <Chip tone="ok">taken over</Chip>) : null}>
      {handover ? (
        <div className="cell-stack" style={{ gap: 4 }}>
          <span className="small faint">
            {handover.created_by} · {handover.created_at ? clock(handover.created_at) : ""} · {handover.open_incidents.length} open then
            {handover.taken_by ? ` · taken over by ${handover.taken_by} at ${handover.taken_at ? clock(handover.taken_at) : ""}` : ""}
          </span>
          {handover.notes ? <p style={{ margin: 0, whiteSpace: "pre-wrap" }}>{handover.notes}</p> : <span className="small faint">No notes.</span>}
        </div>
      ) : <span className="small faint">No handover yet.</span>}
      {canWork ? (
        writing ? (
          <div className="cell-stack" style={{ gap: 6 }}>
            <textarea aria-label="Handover notes" rows={3} value={notes} maxLength={4000} placeholder="What is going on, what to watch, what was promised"
              onChange={(e) => setNotes(e.target.value)} style={{ width: "100%", font: "inherit" }} />
            <div className="row" style={{ gap: 8 }}>
              <button className="btn primary" type="button" onClick={() => void act(() => postJson("/api/v1/handover", { notes }))}>Hand over</button>
              <button className="btn" type="button" onClick={() => setWriting(false)}>Cancel</button>
            </div>
          </div>
        ) : (
          <div className="row" style={{ gap: 8 }}>
            {pending && handover!.created_by !== me ? (
              <button className="btn primary" type="button" onClick={() => void act(() => postJson(`/api/v1/handover/${handover!.id}/take`, {}))}>I take over</button>
            ) : null}
            <button className="btn" type="button" onClick={() => setWriting(true)}>Write handover</button>
          </div>
        )
      ) : null}
      {error ? <span className="small" role="alert" style={{ color: "var(--crit)" }}>{error}</span> : null}
    </Card>
  );
}
