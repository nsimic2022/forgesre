import { useEffect, useState } from "react";
import { getJson, postJson, type Me } from "../lib/api";
import { clock } from "../lib/format";
import { CallLog, Rate, type Call } from "./ForgeAiCard";
import { Card, Chip } from "./ui";

type SkillRow = { slug: string; name: string; task: string; enabled: boolean };
const ROLE_RANK: Record<string, number> = { viewer: 0, analyst: 1, engineer: 2, admin: 3 };

/** The answer of one call as readable text, whatever the task. */
export function AnswerView({ call }: { call: Call }) {
  const a = call.answer as Record<string, unknown>;
  if (call.status === "error" || (call.status === "rejected" && !a?.cause && !a?.answer)) {
    return <span className="small muted">Not answered ({call.status}): {call.reason || "no answer"}</span>;
  }
  const list = (key: string) => (Array.isArray(a[key]) ? (a[key] as string[]) : []);
  return (
    <div className="cell-stack" style={{ gap: 4 }}>
      {call.status === "rejected" ? <span className="small" style={{ color: "var(--warn)" }}>The checks rejected this answer: {call.reason}</span> : null}
      {a.cause ? <span><span className="faint">Likely cause: </span>{String(a.cause)}</span> : null}
      {list("first_checks").length ? <ol style={{ margin: 0, paddingLeft: 18 }}>{list("first_checks").map((c, i) => <li key={i}>{c}</li>)}</ol> : null}
      {a.for_shift_lead ? <span className="small muted">For the shift lead: {String(a.for_shift_lead)}</span> : null}
      {a.answer ? <span style={{ lineHeight: 1.6 }}>{String(a.answer)}</span> : null}
      {list("not_in_facts").length ? (
        <span className="small muted">Not in the facts: {list("not_in_facts").join(" · ")}</span>
      ) : null}
    </div>
  );
}

/** The operator's question from the stored prompt (the prompt may be cut, so no full JSON parse). */
function questionOf(c: Call): string {
  const m = /"operator_question":\s*"((?:[^"\\]|\\.)*)"/.exec(c.user_prompt || "");
  if (!m) return "your question";
  try {
    return JSON.parse(`"${m[1]}"`) as string;
  } catch {
    return m[1];
  }
}

async function waitForCall(job: number): Promise<number | null> {
  for (let i = 0; i < 160; i++) {
    const s = await getJson<{ status: string; error: string; call_id: number | null }>(`/api/v1/forgeai/jobs/${job}`);
    if (s.status === "done") return s.call_id;
    if (s.status === "error") throw new Error(s.error || "ForgeAI failed.");
    await new Promise((r) => setTimeout(r, 2500));
  }
  throw new Error("Still waiting for the model; the answer will show here when it comes.");
}

/**
 * Ask ForgeAI about one incident: what the automatic run asked and got, the same question with
 * another skill side by side, and the operator's own question. Answers here are trials: they never
 * replace the incident's answer unless an Engineer presses "Use this answer".
 */
export function AskForgeAi({ number }: { number: string }) {
  const [me, setMe] = useState<Me | null>(null);
  const [skills, setSkills] = useState<SkillRow[]>([]);
  const [calls, setCalls] = useState<Call[] | null>(null);
  const [skill, setSkill] = useState("");
  const [question, setQuestion] = useState("");
  const [busy, setBusy] = useState("");
  const [error, setError] = useState("");
  const [compareId, setCompareId] = useState<number | null>(null);
  const [askedId, setAskedId] = useState<number | null>(null);

  const load = () =>
    getJson<{ calls: Call[] }>(`/api/v1/incidents/${encodeURIComponent(number)}/forgeai`).then((d) => setCalls(d.calls)).catch((e: Error) => (setCalls([]), setError(e.message)));
  useEffect(() => void getJson<Me>("/api/v1/me").then(setMe).catch(() => setMe(null)), []);
  useEffect(() => void getJson<{ skills: SkillRow[] }>("/api/v1/forgeai/skills").then((d) => setSkills(d.skills.filter((s) => s.enabled && (s.task === "incident" || s.task === "ask")))).catch(() => setSkills([])), []);
  useEffect(() => {
    setCalls(null);
    setCompareId(null);
    setAskedId(null);
    setError("");
    void load();
  }, [number]);

  const rank = ROLE_RANK[me?.role ?? "viewer"] ?? 0;
  const canAsk = rank >= 1;
  const canUse = rank >= 2;
  const automatic = (calls ?? []).find((c) => c.task === "incident" && c.status !== "trial") ?? null;
  const compared = (calls ?? []).find((c) => c.id === compareId) ?? null;
  const asked = (calls ?? []).find((c) => c.id === askedId) ?? null;
  const history = (calls ?? []).filter((c) => c.status === "trial" || c.task === "ask" || c.reason.startsWith("chosen by"));

  const send = async (body: { skill: string; question: string }, label: string, done: (id: number | null) => void) => {
    setBusy(label);
    setError("");
    try {
      const { job } = await postJson<{ job: number }>(`/api/v1/incidents/${encodeURIComponent(number)}/forgeai/ask`, body);
      done(await waitForCall(job));
      await load();
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setBusy("");
    }
  };
  const use = (id: number) =>
    postJson(`/api/v1/forgeai/calls/${id}/use`, {}).then(load).catch((e: Error) => setError(e.message));

  const incidentSkills = skills.filter((s) => s.task === "incident");
  return (
    <div className="cell-stack" style={{ gap: 16 }}>
      <div className="cols">
        <div style={{ flex: "1 1 420px", minWidth: 0 }}>
          <Card title="1 · What ForgeAI was asked on its own">
            {calls === null ? <span className="muted">Loading…</span> : automatic ? (
              <>
                <span className="small faint">
                  <Chip tone={automatic.status === "used" ? "ok" : automatic.status === "rejected" ? "warn" : "crit"}>{automatic.status}</Chip>{" "}
                  skill {automatic.skill} · {automatic.model || "no model"} · {(automatic.duration_ms / 1000).toFixed(1)} s · {clock(automatic.at)}
                </span>
                <AnswerView call={automatic} />
                <CallLog calls={[automatic]} />
              </>
            ) : <span className="muted">ForgeAI did not answer this incident on its own (it was off, or the incident is older).</span>}
          </Card>
        </div>
        <div style={{ flex: "1 1 420px", minWidth: 0 }}>
          <Card title="2 · Ask again with another skill">
            {canAsk ? (
              <div className="row" style={{ gap: 8, flexWrap: "wrap" }}>
                <select className="search" aria-label="Skill to compare" value={skill} onChange={(e) => setSkill(e.target.value)} style={{ minWidth: 0 }}>
                  <option value="">Pick a skill…</option>
                  {incidentSkills.map((s) => <option key={s.slug} value={s.slug}>{s.name}</option>)}
                </select>
                <button className="btn primary" type="button" disabled={!skill || Boolean(busy)} onClick={() => void send({ skill, question: "" }, "compare", setCompareId)}>
                  {busy === "compare" ? "Asking…" : "Ask with this skill"}
                </button>
              </div>
            ) : <span className="small faint">Analysts and up can ask.</span>}
            {compared ? (
              <div className="cell-stack" style={{ gap: 6, borderTop: "1px solid var(--border)", paddingTop: 8 }}>
                <span className="small faint">Skill {compared.skill} · {(compared.duration_ms / 1000).toFixed(1)} s · {clock(compared.at)}</span>
                <AnswerView call={compared} />
                <div className="row" style={{ justifyContent: "space-between" }}>
                  {canUse && compared.status === "trial" ? (
                    <button className="btn" type="button" onClick={() => void use(compared.id)}>Use this answer</button>
                  ) : <span className="small faint">{compared.status === "used" ? "Now the incident's answer." : "A trial: the incident keeps its answer."}</span>}
                  {canAsk ? <Rate callId={compared.id} initial={compared.rating} /> : null}
                </div>
              </div>
            ) : <span className="small faint">The new answer shows here, next to the automatic one, so you can see which skill reads this kind of problem better.</span>}
          </Card>
        </div>
      </div>
      <Card title="3 · Your question">
        {canAsk ? (
          <>
            <textarea className="search" aria-label="Your question" rows={3} maxLength={500} style={{ width: "100%", minWidth: 0, resize: "vertical" }}
              placeholder="For example: could the backup that started at 02:00 have caused this?" value={question} onChange={(e) => setQuestion(e.target.value)} />
            <div className="row" style={{ justifyContent: "space-between" }}>
              <span className="small faint">ForgeAI gets the same facts as the automatic run, plus your question. {question.length}/500</span>
              <button className="btn primary" type="button" disabled={!question.trim() || Boolean(busy)} onClick={() => void send({ skill: "", question }, "ask", setAskedId)}>
                {busy === "ask" ? "Asking…" : "Ask ForgeAI"}
              </button>
            </div>
          </>
        ) : <span className="small faint">Analysts and up can ask.</span>}
        {asked ? (
          <div className="cell-stack" style={{ gap: 6, borderTop: "1px solid var(--border)", paddingTop: 8 }}>
            <AnswerView call={asked} />
            {canAsk ? <div className="row" style={{ justifyContent: "flex-end" }}><Rate callId={asked.id} initial={asked.rating} /></div> : null}
          </div>
        ) : null}
        {busy ? <span className="small faint">The local model takes 30–90 seconds. You can leave; the answer is kept below.</span> : null}
        {error ? <span className="small" role="alert" style={{ color: "var(--crit)" }}>{error}</span> : null}
      </Card>
      {history.length ? (
        <Card title={`Earlier questions · ${history.length}`}>
          {history.map((c) => (
            <details key={c.id} style={{ borderTop: "1px solid var(--border)", paddingTop: 6 }}>
              <summary className="small" style={{ cursor: "pointer" }}>
                {c.task === "ask" ? `“${questionOf(c)}”` : `Skill ${c.skill}`} · {c.actor} · {clock(c.at)}
              </summary>
              <div style={{ paddingTop: 6 }}><AnswerView call={c} /></div>
            </details>
          ))}
        </Card>
      ) : null}
    </div>
  );
}
