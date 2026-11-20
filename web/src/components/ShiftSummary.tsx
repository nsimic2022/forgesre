import { useState } from "react";
import { ApiError, getJson, postJson } from "../lib/api";
import { CallLog, Rate, type Call } from "./ForgeAiCard";
import { Card } from "./ui";

type Answer = { summary: string; still_open: string[]; watch: string[] };

/** NOC: ForgeAI writes the handover for the next shift from the last 12 hours of incidents. */
export function ShiftSummary() {
  const [call, setCall] = useState<Call | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [hidden, setHidden] = useState(false);
  const run = async () => {
    setBusy(true);
    setError("");
    try {
      const { job } = await postJson<{ job: number }>("/api/v1/forgeai/shift-summary", {});
      for (let i = 0; i < 120; i++) {
        const st = await getJson<{ status: string; error: string; call_id: number | null }>(`/api/v1/forgeai/jobs/${job}`);
        if (st.status === "error") throw new Error(st.error || "ForgeAI failed.");
        if (st.status === "done" && st.call_id) {
          setCall(await getJson<Call>(`/api/v1/forgeai/calls/${st.call_id}`));
          break;
        }
        await new Promise((r) => setTimeout(r, 2500));
      }
    } catch (e) {
      if (e instanceof ApiError && e.status === 403) setHidden(true);
      else setError((e as Error).message);
    } finally {
      setBusy(false);
    }
  };
  if (hidden) return null;
  const a = call?.status === "used" ? (call.answer as Answer) : null;
  return (
    <Card title="Shift handover · ForgeAI" actions={<button className="btn" type="button" onClick={run} disabled={busy}>{busy ? "Writing (30–90 s)…" : call ? "Write again" : "Write the handover"}</button>}>
      {!call && !busy && !error ? <span className="small faint">The last 12 hours in a few sentences for the next shift: what happened, what is still open, what to watch.</span> : null}
      {error ? <span className="small" role="alert" style={{ color: "var(--crit)" }}>{error}</span> : null}
      {a ? (
        <>
          <p style={{ margin: 0, lineHeight: 1.6 }}>{a.summary}</p>
          {a.still_open?.length ? <><strong className="small">Still open</strong><ul style={{ margin: 0, paddingLeft: 18 }}>{a.still_open.map((x, i) => <li key={i}>{x}</li>)}</ul></> : null}
          {a.watch?.length ? <><strong className="small">Keep an eye on</strong><ul style={{ margin: 0, paddingLeft: 18 }}>{a.watch.map((x, i) => <li key={i}>{x}</li>)}</ul></> : null}
          <div className="row small faint" style={{ justifyContent: "space-between" }}><span>Advisory; check it against the incident list.</span><Rate callId={call!.id} /></div>
        </>
      ) : null}
      {call && !a ? <span className="small" style={{ color: "var(--warn)" }}>Not shown: {call.reason || call.status}.</span> : null}
      {call ? <details><summary className="small faint" style={{ cursor: "pointer" }}>What was ForgeAI asked?</summary><CallLog calls={[call]} /></details> : null}
    </Card>
  );
}
