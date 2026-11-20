import { useEffect, useState } from "react";
import { getJson, postJson } from "../lib/api";
import { clock } from "../lib/format";
import { Card, Chip } from "./ui";

export type ForgeAiView = {
  mode: string; text: string; cause: string; action: string; model: string; at: string | null;
  answer?: { cause: string; first_checks: string[]; for_shift_lead: string } | null;
  skill?: string; call_id?: number | null;
  skills?: { slug: string; name: string }[];
  extras?: { explain?: { answer: { explanation: string; why_it_matters: string }; call_id: number }; postmortem?: { answer: { paragraph: string }; call_id: number } };
};
export type Call = {
  id: number; at: string | null; task: string; skill: string; skill_version: number; model: string; status: string; reason: string;
  duration_ms: number; answer: Record<string, unknown>; rating: number; actor: string;
  system_prompt?: string; user_prompt?: string; raw_response?: string;
};

const STATUS_TONE: Record<string, "ok" | "crit" | "warn" | "off"> = { used: "ok", rejected: "warn", error: "crit", trial: "off" };
const TASK: Record<string, string> = { incident: "Cause and first checks", explain: "Explain for Tier 1", postmortem: "Postmortem summary", shift: "Shift summary" };

function pretty(text = ""): string {
  try {
    return JSON.stringify(JSON.parse(text), null, 2);
  } catch {
    return text;
  }
}

/** Thumbs up / down on one answer; counted per skill so a weak skill shows up. */
export function Rate({ callId, initial = 0 }: { callId: number; initial?: number }) {
  const [value, setValue] = useState(initial);
  const set = (v: number) => postJson<{ rating: number }>(`/api/v1/forgeai/calls/${callId}/rate`, { value: value === v ? 0 : v }).then((r) => setValue(r.rating)).catch(() => undefined);
  return (
    <span className="row" style={{ gap: 4 }} aria-label="Was this useful?">
      <button className="btn" type="button" aria-pressed={value === 1} aria-label="Useful" onClick={() => set(1)}>👍</button>
      <button className="btn" type="button" aria-pressed={value === -1} aria-label="Not useful" onClick={() => set(-1)}>👎</button>
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
            <Chip tone={STATUS_TONE[c.status] ?? "off"}>{c.status}</Chip> {TASK[c.task] ?? c.task} · skill {c.skill} v{c.skill_version} · {c.model || "no model"} ·{" "}
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
      for (let i = 0; i < 120; i++) {
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
        <span className="muted">{off ? "ForgeAI is off in Settings." : "No ForgeAI opinion yet; it is written after ForgeRCA finishes (30–90 s on the local model)."}</span>
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
      {canAsk && !off ? (
        <div className="row" style={{ gap: 6, flexWrap: "wrap" }}>
          <select className="search" aria-label="Skill" value={skill} onChange={(e) => setSkill(e.target.value)} style={{ minWidth: 0 }}>
            <option value="">Skill: picked by ForgeAI</option>
            {(view.skills ?? []).map((s) => <option key={s.slug} value={s.slug}>{s.name}</option>)}
          </select>
          <button className="btn" type="button" disabled={Boolean(busy)} onClick={() => ask("incident", "Asking…")}>{busy === "Asking…" ? busy : a ? "Ask again" : "Ask ForgeAI"}</button>
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
