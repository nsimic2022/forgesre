import { useEffect, useState } from "react";
import { Link } from "react-router-dom";
import { getJson, postJson, type Handover, type MajorBrief } from "../lib/api";
import { ShiftSummary } from "./ShiftSummary";
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
/** The handover part of the Shift card: the latest handover, take it over, or write one. */
export function HandoverPart({ handover, canWork, me, onChanged, draft }: { handover: Handover | null | undefined; canWork: boolean; me: string; onChanged: () => void; draft?: string }) {
  const [writing, setWriting] = useState(false);
  const [notes, setNotes] = useState("");
  const [error, setError] = useState("");
  useEffect(() => {
    if (draft) (setNotes(draft), setWriting(true));
  }, [draft]);
  const act = (fn: () => Promise<unknown>) => fn().then(() => (setWriting(false), setNotes(""), onChanged())).catch((e: Error) => setError(e.message));
  const pending = handover && !handover.taken_by;
  return (
    <div className="cell-stack shift-part" style={{ gap: 8 }}>
      <div className="row" style={{ justifyContent: "space-between" }}>
        <strong className="small">Handover</strong>
        {handover ? (pending ? <Chip tone="warn">not taken over</Chip> : <Chip tone="ok">taken over</Chip>) : null}
      </div>
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
            <textarea aria-label="Handover notes" rows={5} value={notes} maxLength={4000} placeholder="What is going on, what to watch, what was promised"
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
    </div>
  );
}

type OnCallNow = { person: string; window?: string; phone?: string; email?: string } | null | undefined;

/**
 * Shift (Incidents › On-call): one card for the change of shift. Who is on call now, the handover
 * people write and take over, and the ForgeAI draft that can fill the handover. Loads the latest
 * handover itself; the NOC shows only a one-line summary that links here.
 */
export function ShiftPanel({ now, canWork, me }: { now: OnCallNow; canWork: boolean; me: string }) {
  const [handover, setHandover] = useState<Handover | null | undefined>(undefined);
  const [ai, setAi] = useState(false);
  const [draft, setDraft] = useState("");
  const load = () => getJson<{ handovers: Handover[] }>("/api/v1/handover").then((r) => setHandover(r.handovers[0] ?? null)).catch(() => setHandover(null));
  useEffect(() => {
    void load();
    getJson<{ enabled?: boolean }>("/api/v1/forgeai/status").then((s) => setAi(Boolean(s.enabled))).catch(() => setAi(false));
  }, []);
  return (
    <Card title="Shift">
      <div className="shift-grid">
        <div className="cell-stack shift-part" style={{ gap: 6 }}>
          <strong className="small">On call now</strong>
          {now ? (
            <>
              <span style={{ fontWeight: 600, fontSize: "1.1em" }}>{now.person}</span>
              <span className="small muted">{[now.window, now.phone, now.email].filter(Boolean).join(" · ")}</span>
            </>
          ) : <span className="muted small">Nobody on the roster for this minute. Escalation to “oncall” falls back to the asset owner.</span>}
        </div>
        <HandoverPart handover={handover} canWork={canWork} me={me} onChanged={load} draft={draft} />
        {ai && canWork ? <ShiftSummary onUse={(text) => setDraft(text)} /> : null}
      </div>
    </Card>
  );
}

/** NOC: one line about the shift (who is on call, handover state) linking to Incidents › On-call. */
export function ShiftLine({ oncall, handover }: { oncall: { name: string; phone?: string } | null | undefined; handover: Handover | null | undefined }) {
  const pending = handover && !handover.taken_by;
  return (
    <div className="shift-line" role="group" aria-label="Shift">
      <span className="small muted">On call now</span>
      <strong>{oncall ? oncall.name : "nobody"}</strong>
      {oncall?.phone ? <span className="small muted">{oncall.phone}</span> : null}
      <span className="small muted" style={{ marginLeft: 12 }}>Handover</span>
      {handover ? (pending ? <Chip tone="warn">not taken over</Chip> : <Chip tone="ok">taken over</Chip>) : <span className="small faint">none yet</span>}
      <Link className="small" to="/incidents/on-call" style={{ marginLeft: "auto" }}>Shift and schedule →</Link>
    </div>
  );
}
