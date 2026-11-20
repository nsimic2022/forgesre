import { useEffect, useRef, useState } from "react";
import { getJson, postJson } from "../lib/api";
import { clock } from "../lib/format";
import { Card, Chip } from "./ui";

type Draft = {
  subject: string; body: string; smtp: boolean;
  suggestions: { email: string; label: string }[];
  sent: { to: string; status: string; at: string | null }[];
};
type Sent = { to: string; status: string; error: string };
type JobView = { status: string; error: string; result: Sent[] | null };

const TONE: Record<string, "ok" | "crit" | "off"> = { sent: "ok", failed: "crit", generated: "off" };

/** Send the incident report by mail. The server sends it in a background job (one outbox row per
 *  address); this waits for the job and shows sent, failed (with the SMTP error) or generated. */
export function SendReport({ number, onClose }: { number: string; onClose: () => void }) {
  const base = `/api/v1/incidents/${encodeURIComponent(number)}/mail`;
  const [draft, setDraft] = useState<Draft | null>(null);
  const [to, setTo] = useState("");
  const [subject, setSubject] = useState("");
  const [body, setBody] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [result, setResult] = useState<Sent[] | null>(null);
  const alive = useRef(true);
  useEffect(() => () => void (alive.current = false), []);

  useEffect(() => {
    getJson<Draft>(base)
      .then((d) => (setDraft(d), setSubject(d.subject), setBody(d.body)))
      .catch((e: Error) => setError(e.message));
  }, [base]);

  const add = (email: string) => {
    const list = to.split(/[,;]/).map((s) => s.trim()).filter(Boolean);
    if (!list.some((s) => s.toLowerCase() === email.toLowerCase())) setTo([...list, email].join(", "));
  };
  const send = async () => {
    setBusy(true);
    setError("");
    setResult(null);
    try {
      const edited = draft && body.trim() !== draft.body.trim();
      const started = await postJson<{ job: number }>(base, { to, subject, body: edited ? body : "" });
      for (let i = 0; i < 60 && alive.current; i++) {
        const job = await getJson<JobView>(`${base}/${started.job}`);
        if (job.status === "done") return void setResult(job.result ?? []);
        if (job.status === "error") return void setError(job.error || "Sending failed.");
        await new Promise((r) => setTimeout(r, 1000));
      }
      if (alive.current) setError("Still sending; the Outbox on the Reports page shows the result.");
    } catch (e) {
      if (alive.current) setError((e as Error).message);
    } finally {
      if (alive.current) setBusy(false);
    }
  };

  return (
    <Card title="Send report" actions={<button className="btn" type="button" onClick={onClose}>Close</button>}>
      {!draft && !error ? <span className="muted">Loading…</span> : null}
      {draft && !draft.smtp ? (
        <div className="banner" role="status" style={{ background: "var(--warn-bg)" }}>
          SMTP is off: the report is kept in the outbox but not sent. Turn it on in Settings › Notifications.
        </div>
      ) : null}
      {draft ? (
        <>
          <label className="cell-stack" style={{ gap: 4 }}>
            <span className="small faint">To (comma between addresses, up to 10)</span>
            <input aria-label="To" value={to} onChange={(e) => setTo(e.target.value)} placeholder="ops@example.com" />
          </label>
          {draft.suggestions.length ? (
            <div className="row" style={{ gap: 6 }}>
              {draft.suggestions.slice(0, 8).map((s) => (
                <button key={s.email} className="btn" type="button" title={s.label} onClick={() => add(s.email)}>+ {s.email}</button>
              ))}
            </div>
          ) : null}
          <label className="cell-stack" style={{ gap: 4 }}>
            <span className="small faint">Subject</span>
            <input aria-label="Subject" value={subject} onChange={(e) => setSubject(e.target.value)} />
          </label>
          <label className="cell-stack" style={{ gap: 4 }}>
            <span className="small faint">Report (left as it is, it also goes as formatted HTML; edited, as plain text)</span>
            <textarea aria-label="Report" className="mono" rows={12} value={body} onChange={(e) => setBody(e.target.value)} />
          </label>
          <div className="row">
            <button className="btn primary" type="button" disabled={busy || !to.trim()} onClick={() => void send()}>{busy ? "Sending…" : "Send"}</button>
            {body !== draft.body ? <button className="btn" type="button" onClick={() => setBody(draft.body)}>Back to the generated report</button> : null}
          </div>
          {draft.sent.length ? (
            <span className="small muted">Sent before: {draft.sent.map((s) => `${s.to} (${s.status}${s.at ? `, ${clock(s.at)}` : ""})`).join(" · ")}</span>
          ) : null}
        </>
      ) : null}
      {error ? <div className="banner error" role="alert">{error}</div> : null}
      {result ? (
        <div className="cell-stack" style={{ gap: 4 }} role="status">
          {result.map((r) => (
            <span key={r.to} className="row small" style={{ gap: 8 }}>
              <Chip tone={TONE[r.status] ?? "off"}>{r.status}</Chip>{r.to}{r.error ? <span className="muted">{r.error}</span> : null}
            </span>
          ))}
        </div>
      ) : null}
    </Card>
  );
}
