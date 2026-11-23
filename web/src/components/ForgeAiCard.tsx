import { useEffect, useState } from "react";
import { getJson, postJson } from "../lib/api";
import { clock } from "../lib/format";
import { Card, Chip } from "./ui";

export type ForgeAiView = {
  mode: string; text: string; cause: string; action: string; model: string; at: string | null;
  answer?: { cause: string; first_checks: string[]; for_shift_lead: string } | null;
  skill?: string; call_id?: number | null;
  last?: { status: string; reason: string; at: string | null; skill: string } | null;
  deep?: {
    call_id: number; status: string; reason: string; at: string | null; model: string; seconds: number; evidence_count: number;
    answer: { cause?: string; confidence?: string; evidence?: string[]; next_steps?: string[] };
    cited: { id: string; source: string; text: string }[];
  } | null;
  skills?: { slug: string; name: string }[];
  extras?: { explain?: { answer: { explanation: string; why_it_matters: string }; call_id: number }; postmortem?: { answer: { paragraph: string }; call_id: number } };
};
export type Call = {
  id: number; at: string | null; task: string; skill: string; skill_version: number; model: string; status: string; reason: string;
  duration_ms: number; answer: Record<string, unknown>; rating: number; stars?: number; actor: string; cloud?: boolean;
  system_prompt?: string; user_prompt?: string; raw_response?: string;
};

const STATUS_TONE: Record<string, "ok" | "crit" | "warn" | "off"> = { used: "ok", rejected: "warn", error: "crit", trial: "off" };
const TASK: Record<string, string> = { incident: "Cause and first checks", investigate: "Investigation with tools", explain: "Explain for Tier 1", postmortem: "Postmortem summary", ask: "Your question", shift: "Shift summary" };

function pretty(text = ""): string {
  try {
    return JSON.stringify(JSON.parse(text), null, 2);
  } catch {
    return text;
  }
}

const STAR_LABEL = ["", "Wrong or useless", "Weak", "Partly right", "Good", "Spot on"];

/**
 * 1–5 stars on one answer (click the same star again to clear); counted per skill so a weak skill
 * shows up. 4–5 stars also count as thumbs up and 1–2 as thumbs down in older reports.
 */
export function Rate({ callId, initial = 0 }: { callId: number; initial?: number }) {
  const [stars, setStars] = useState(initial);
  const [hover, setHover] = useState(0);
  const set = (v: number) =>
    postJson<{ stars: number }>(`/api/v1/forgeai/calls/${callId}/rate`, { stars: stars === v ? 0 : v }).then((r) => setStars(r.stars ?? 0)).catch(() => undefined);
  const shown = hover || stars;
  return (
    <span className="stars" role="group" aria-label="Rate this answer" onMouseLeave={() => setHover(0)}>
      {[1, 2, 3, 4, 5].map((n) => (
        <button key={n} type="button" className={n <= shown ? "on" : ""} aria-pressed={n <= stars} aria-label={`${n} of 5: ${STAR_LABEL[n]}`}
          title={STAR_LABEL[n]} onMouseEnter={() => setHover(n)} onFocus={() => setHover(n)} onBlur={() => setHover(0)} onClick={() => void set(n)}>
          ★
        </button>
      ))}
      {shown ? <span className="small faint">{STAR_LABEL[shown]}</span> : null}
    </span>
  );
}

/** Every question put to the model for this incident: exact prompts, raw answer, outcome, time. */
export function CallLog({ calls }: { calls: Call[] }) {
  if (!calls.length) return <span className="small faint">ForgeAI has not been asked anything about this incident yet.</span>;
  return (
    <div className="cell-stack" style={{ gap: 8 }}>
      {calls.map((c) => (
        <details key={c.id} style={{ borderTop: "1px solid var(--border)", paddingTop: 6 }}>
          <summary style={{ cursor: "pointer" }} className="small">
            <Chip tone={STATUS_TONE[c.status] ?? "off"}>{c.status}</Chip> {c.cloud ? <Chip tone="warn">cloud</Chip> : null} {TASK[c.task] ?? c.task} · skill {c.skill} v{c.skill_version} · {c.model || "no model"} ·{" "}
            {(c.duration_ms / 1000).toFixed(1)} s · {clock(c.at)}{c.reason ? ` · ${c.reason}` : ""}
          </summary>
          {c.system_prompt !== undefined ? (
            <div className="cell-stack" style={{ gap: 6, marginTop: 6 }}>
              <span className="small faint">System prompt: fixed rules, the answer shape and the skill</span>
              <pre className="mono small" style={{ whiteSpace: "pre-wrap", margin: 0, maxHeight: 220, overflow: "auto" }}>{c.system_prompt}</pre>
              <span className="small faint">What ForgeAI was given (secrets removed)</span>
              <pre className="mono small" style={{ whiteSpace: "pre-wrap", margin: 0, maxHeight: 260, overflow: "auto" }}>{pretty(c.user_prompt)}</pre>
              <span className="small faint">What the model answered, word for word</span>
              <pre className="mono small" style={{ whiteSpace: "pre-wrap", margin: 0, maxHeight: 220, overflow: "auto" }}>{c.raw_response || "(nothing)"}</pre>
            </div>
          ) : <span className="small faint">Prompts are shown to Analysts and up.</span>}
        </details>
      ))}
    </div>
  );
}

/** ForgeAI on the incident page: the checked answer, explain / postmortem, ask again with another skill, the call log. */
export function ForgeAiCard({ number, view, canAsk, onChanged }: { number: string; view: ForgeAiView; canAsk: boolean; onChanged: () => void }) {
  const [skill, setSkill] = useState("");
  const [busy, setBusy] = useState("");
  const [error, setError] = useState("");
  const [calls, setCalls] = useState<Call[] | null>(null);
  const [showLog, setShowLog] = useState(false);
  const loadCalls = () => getJson<{ calls: Call[] }>(`/api/v1/incidents/${encodeURIComponent(number)}/forgeai`).then((d) => setCalls(d.calls)).catch(() => setCalls([]));
  useEffect(() => {
    if (showLog) void loadCalls();
  }, [showLog, number]);
  const ask = async (task: string, label: string) => {
    setBusy(label);
    setError("");
    try {
      const { job } = await postJson<{ job: number }>(`/api/v1/incidents/${encodeURIComponent(number)}/forgeai`, { task, skill: task === "incident" ? skill : "" });
      for (let i = 0; i < (task === "investigate" ? 160 : 120); i++) {
        const state = await getJson<{ status: string; error: string }>(`/api/v1/forgeai/jobs/${job}`);
        if (state.status === "done" || state.status === "error") {
          if (state.status === "error") setError(state.error || "ForgeAI failed.");
          break;
        }
        await new Promise((r) => setTimeout(r, 2500));
      }
      onChanged();
      if (showLog) void loadCalls();
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setBusy("");
    }
  };
  const off = view.mode === "off";
  const a = view.answer;
  return (
    <Card title="ForgeAI · opinion" actions={<Chip tone={off ? "off" : "info"}>{off ? "Off" : view.mode === "external" ? "External model" : "Local model"}</Chip>}>
      {a ? (
        <>
          <p style={{ margin: 0, lineHeight: 1.6 }}><span className="faint">Likely cause: </span>{a.cause}</p>
          {a.first_checks?.length ? (
            <ol style={{ margin: 0, paddingLeft: 18, display: "flex", flexDirection: "column", gap: 4 }}>{a.first_checks.map((c, i) => <li key={i}>{c}</li>)}</ol>
          ) : null}
          <p style={{ margin: 0 }}><span className="faint">For the shift lead: </span>{a.for_shift_lead}</p>
        </>
      ) : view.text ? (
        <p style={{ margin: 0, lineHeight: 1.6 }}>{view.text}</p>
      ) : (
        <span className="muted">
          {off
            ? "ForgeAI is off in Settings."
            : view.last && view.last.status !== "used"
              ? `Last question (${clock(view.last.at)}, skill ${view.last.skill}) was ${view.last.status === "rejected" ? "rejected" : "not answered"}: ${view.last.reason}. Ask again, or see what it was asked below.`
              : view.last
                ? "ForgeAI is writing its opinion (30–90 s on the local model)."
                : "ForgeAI has not been asked about this incident yet. New incidents get an answer on their own after ForgeRCA; press Ask ForgeAI for this one."}
        </span>
      )}
      {a || view.text ? (
        <div className="row small faint" style={{ justifyContent: "space-between" }}>
          <span>Advisory. {view.skill ? `Skill: ${view.skill}. ` : ""}{view.model ? `Model: ${view.model}. ` : ""}{clock(view.at)}</span>
          {view.call_id && canAsk ? <Rate callId={view.call_id} /> : null}
        </div>
      ) : null}
      {view.extras?.explain ? (
        <div className="cell-stack" style={{ gap: 4, borderTop: "1px solid var(--border)", paddingTop: 8 }}>
          <span className="small faint">In plain words</span>
          <span>{view.extras.explain.answer.explanation}</span>
          <span className="small muted">{view.extras.explain.answer.why_it_matters}</span>
        </div>
      ) : null}
      {view.extras?.postmortem ? (
        <div className="cell-stack" style={{ gap: 4, borderTop: "1px solid var(--border)", paddingTop: 8 }}>
          <span className="small faint">Postmortem summary (goes into the Postmortem draft)</span>
          <span>{view.extras.postmortem.answer.paragraph}</span>
        </div>
      ) : null}
      {view.deep ? (
        <div className="cell-stack" style={{ gap: 4, borderTop: "1px solid var(--border)", paddingTop: 8 }}>
          <span className="small faint">
            Investigation with tools · {clock(view.deep.at)} · {view.deep.seconds} s · {view.deep.evidence_count} pieces of evidence
          </span>
          {view.deep.status === "used" && view.deep.answer.cause ? (
            <>
              <span><Chip tone={view.deep.answer.confidence === "high" ? "ok" : view.deep.answer.confidence === "medium" ? "info" : "warn"}>{view.deep.answer.confidence} confidence</Chip> {view.deep.answer.cause}</span>
              <ul className="small muted" style={{ margin: 0, paddingLeft: 18 }}>
                {view.deep.cited.map((e) => <li key={e.id}><span className="mono">{e.id}</span> {e.text}</li>)}
              </ul>
              {view.deep.answer.next_steps?.length ? (
                <ol style={{ margin: 0, paddingLeft: 18 }}>{view.deep.answer.next_steps.map((n, i) => <li key={i}>{n}</li>)}</ol>
              ) : null}
              {canAsk ? <div className="row" style={{ justifyContent: "flex-end" }}><Rate callId={view.deep.call_id} /></div> : null}
            </>
          ) : (
            <span className="small muted">Not used ({view.deep.status}): {view.deep.reason}</span>
          )}
        </div>
      ) : null}
      {canAsk && !off ? (
        <div className="row" style={{ gap: 6, flexWrap: "wrap" }}>
          <select className="search" aria-label="Skill" value={skill} onChange={(e) => setSkill(e.target.value)} style={{ minWidth: 0 }}>
            <option value="">Skill: picked by ForgeAI</option>
            {(view.skills ?? []).map((s) => <option key={s.slug} value={s.slug}>{s.name}</option>)}
          </select>
          <button className="btn" type="button" disabled={Boolean(busy)} onClick={() => ask("incident", "Asking…")}>{busy === "Asking…" ? busy : a ? "Ask again" : "Ask ForgeAI"}</button>
          <button className="btn" type="button" disabled={Boolean(busy)} onClick={() => ask("investigate", "Investigating…")} title="Several steps with read-only tools: metrics, logs, interfaces, similar incidents (1–4 min)">{busy === "Investigating…" ? busy : "Investigate with tools"}</button>
          <button className="btn" type="button" disabled={Boolean(busy)} onClick={() => ask("explain", "Explaining…")}>{busy === "Explaining…" ? busy : "Explain for Tier 1"}</button>
          <button className="btn" type="button" disabled={Boolean(busy)} onClick={() => ask("postmortem", "Writing…")}>{busy === "Writing…" ? busy : "Postmortem summary"}</button>
        </div>
      ) : null}
      {busy ? <span className="small faint">The local model takes 30–90 seconds. You can leave this page; the answer stays on the incident.</span> : null}
      {error ? <span className="small" role="alert" style={{ color: "var(--crit)" }}>{error}</span> : null}
      <button className="btn" type="button" style={{ alignSelf: "flex-start" }} aria-expanded={showLog} onClick={() => setShowLog(!showLog)}>
        {showLog ? "Hide" : "What was ForgeAI asked?"}
      </button>
      {showLog ? (calls ? <CallLog calls={calls} /> : <span className="muted">Loading…</span>) : null}
    </Card>
  );
}
