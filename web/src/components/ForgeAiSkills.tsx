import { useEffect, useState } from "react";
import { ApiError, getJson, postJson, putJson } from "../lib/api";
import { clock } from "../lib/format";
import { CallLog, type Call } from "./ForgeAiCard";
import { ForgeAiTuning } from "./ForgeAiTuning";
import { Card, Chip } from "./ui";

type Stats = { calls: number; used: number; rejected: number; errors: number; up: number; down: number; avg_s: number; stars_avg: number | null; stars_n: number };
export type Skill = {
  slug: string; name: string; task: string; task_label: string; match_alerts: string[]; match_types: string[]; priority: number;
  instructions: string; default_instructions: string; include: Record<string, boolean>; enabled: boolean; builtin: boolean;
  version: number; updated_by: string; updated_at: string | null;
  history: { version: number; instructions: string; by: string; at: string | null }[]; stats: Partial<Stats>;
};
type Data = { skills: Skill[]; tasks: Record<string, string>; can_edit: boolean };

const INCLUDE: [string, string][] = [
  ["facts", "ForgeRCA facts and hypotheses"], ["playbook", "The playbook steps"], ["similar", "Earlier incidents on the asset with a confirmed cause"],
  ["changes", "Changes in the 24 h before"], ["syslog", "Up to 20 syslog lines from the device (slower)"],
];

/** How useful a skill was over 30 days, in one line. */
function StatLine({ s }: { s: Partial<Stats> }) {
  if (!s.calls) return <span className="small faint">Not used in the last 30 days.</span>;
  return (
    <span className="small muted">
      {s.calls} questions · {s.used} used · {s.rejected} rejected · {s.errors} no answer · {s.avg_s} s · {s.stars_n ? `★ ${s.stars_avg} (${s.stars_n})` : "not rated yet"}
    </span>
  );
}

function Editor({ skill, canEdit, onSaved }: { skill: Skill; canEdit: boolean; onSaved: (s: Skill) => void }) {
  const [d, setD] = useState(skill);
  const [incident, setIncident] = useState("");
  const [trial, setTrial] = useState<Call | null>(null);
  const [busy, setBusy] = useState("");
  const [error, setError] = useState("");
  useEffect(() => (setD(skill), setTrial(null), setError("")), [skill.slug, skill.version]);
  const incidentTask = skill.task === "incident";
  const save = () => {
    setBusy("Saving…");
    setError("");
    putJson<Skill>(`/api/v1/forgeai/skills/${skill.slug}`, {
      name: d.name, instructions: d.instructions, include: d.include, enabled: d.enabled, priority: d.priority,
      ...(incidentTask ? { match_alerts: d.match_alerts, match_types: d.match_types } : {}),
    }).then(onSaved).catch((e: Error) => setError(e.message)).finally(() => setBusy(""));
  };
  const reset = () => postJson<Skill>(`/api/v1/forgeai/skills/${skill.slug}/reset`, {}).then(onSaved).catch((e: Error) => setError(e.message));
  const tryIt = async () => {
    setBusy("Asking the model (30–90 s)…");
    setError("");
    setTrial(null);
    try {
      const { job } = await postJson<{ job: number }>("/api/v1/forgeai/trial", { incident: incident.trim(), task: skill.task, skill: skill.slug, instructions: d.instructions });
      for (let i = 0; i < 120; i++) {
        const st = await getJson<{ status: string; error: string; call_id: number | null }>(`/api/v1/forgeai/jobs/${job}`);
        if (st.status === "error") throw new Error(st.error || "The trial failed.");
        if (st.status === "done" && st.call_id) {
          setTrial(await getJson<Call>(`/api/v1/forgeai/calls/${st.call_id}`));
          break;
        }
        await new Promise((r) => setTimeout(r, 2500));
      }
    } catch (e) {
      setError(e instanceof ApiError && e.status === 409 ? "ForgeAI is off: turn it on in Settings › RCA / AI." : (e as Error).message);
    } finally {
      setBusy("");
    }
  };
  const list = (v: string[]) => v.join(", ");
  const split = (v: string) => v.split(",").map((x) => x.trim()).filter(Boolean);
  const changed = d.instructions !== skill.instructions;
  return (
    <>
    <Card title={`${skill.name} · v${skill.version}`} actions={<Chip tone={skill.enabled ? "ok" : "off"}>{skill.enabled ? "On" : "Off"}</Chip>}>
      <StatLine s={skill.stats} />
      <span className="small faint">{skill.builtin ? "Shipped with ForgeSRE" : "Added here"} · last change {clock(skill.updated_at)} by {skill.updated_by}</span>
      <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fill, minmax(240px, 1fr))", gap: 10 }}>
        <label className="cell-stack small faint">Name<input className="search" style={{ minWidth: 0 }} disabled={!canEdit} value={d.name} onChange={(e) => setD({ ...d, name: e.target.value })} /></label>
        {incidentTask ? (
          <>
            <label className="cell-stack small faint">Alarm names (patterns, comma separated; empty = any)
              <input className="search" style={{ minWidth: 0 }} disabled={!canEdit} value={list(d.match_alerts)} onChange={(e) => setD({ ...d, match_alerts: split(e.target.value) })} placeholder="*disk*, *filesystem*" />
            </label>
            <label className="cell-stack small faint">Asset types (comma separated; empty = any)
              <input className="search" style={{ minWidth: 0 }} disabled={!canEdit} value={list(d.match_types)} onChange={(e) => setD({ ...d, match_types: split(e.target.value) })} placeholder="Linux Server, Oracle DB" />
            </label>
            <label className="cell-stack small faint">Order (lower wins when two match)
              <input className="search" style={{ minWidth: 0 }} type="number" disabled={!canEdit} value={d.priority} onChange={(e) => setD({ ...d, priority: Number(e.target.value) })} />
            </label>
          </>
        ) : null}
      </div>
      <label className="cell-stack small faint">
        Instructions to ForgeAI: write them like a note to a colleague. The safety rules and the answer shape are added by ForgeSRE and cannot be changed here.
        <textarea className="search" rows={7} disabled={!canEdit} value={d.instructions} onChange={(e) => setD({ ...d, instructions: e.target.value })} style={{ minWidth: 0, fontFamily: "inherit", lineHeight: 1.5 }} />
      </label>
      <fieldset style={{ border: "1px solid var(--border)", borderRadius: 10, padding: 10, margin: 0 }}>
        <legend className="small faint" style={{ padding: "0 6px" }}>What ForgeAI is given</legend>
        {INCLUDE.map(([k, l]) => (
          <label key={k} className="row small" style={{ gap: 8 }}>
            <input type="checkbox" disabled={!canEdit} checked={Boolean(d.include[k])} onChange={(e) => setD({ ...d, include: { ...d.include, [k]: e.target.checked } })} />{l}
          </label>
        ))}
        <span className="small faint">Always: the incident and the asset. Passwords, tokens and SNMP secrets are removed first.</span>
      </fieldset>
      {canEdit ? (
        <div className="row" style={{ gap: 6, flexWrap: "wrap" }}>
          <button className="btn primary" type="button" onClick={save} disabled={Boolean(busy)}>Save as v{skill.version + 1}</button>
          <label className="row small" style={{ gap: 6 }}><input type="checkbox" checked={d.enabled} onChange={(e) => setD({ ...d, enabled: e.target.checked })} />On</label>
          {skill.builtin && skill.instructions !== skill.default_instructions ? <button className="btn" type="button" onClick={reset}>Reset to default</button> : null}
          <span className="faint">·</span>
          {skill.task !== "shift" ? <input className="search" aria-label="Incident to try it on" placeholder="INC-… (empty = newest)" value={incident} onChange={(e) => setIncident(e.target.value)} style={{ width: 200, minWidth: 0 }} /> : null}
          <button className="btn" type="button" onClick={tryIt} disabled={Boolean(busy)}>{changed ? "Try the unsaved text" : "Try it"}</button>
        </div>
      ) : null}
      {busy ? <span className="small faint">{busy}</span> : null}
      {error ? <span className="small" role="alert" style={{ color: "var(--crit)" }}>{error}</span> : null}
      {trial ? (
        <div className="cell-stack" style={{ gap: 6, borderTop: "1px solid var(--border)", paddingTop: 8 }}>
          <strong className="small">Trial answer {trial.status === "trial" ? "(passed the checks; not shown on the incident)" : `(${trial.status}: ${trial.reason})`}</strong>
          <pre className="mono small" style={{ whiteSpace: "pre-wrap", margin: 0 }}>{JSON.stringify(trial.answer, null, 2)}</pre>
          <CallLog calls={[trial]} />
        </div>
      ) : null}
      {skill.history.length ? (
        <details>
          <summary className="small faint" style={{ cursor: "pointer" }}>Earlier versions ({skill.history.length})</summary>
          {skill.history.slice().reverse().map((h) => (
            <div key={h.version} className="cell-stack" style={{ gap: 4, borderTop: "1px solid var(--border)", paddingTop: 6 }}>
              <span className="small faint">v{h.version} · {clock(h.at)} · {h.by}</span>
              <pre className="small" style={{ whiteSpace: "pre-wrap", margin: 0, fontFamily: "inherit" }}>{h.instructions}</pre>
              {canEdit ? <button className="btn" type="button" style={{ alignSelf: "flex-start" }} onClick={() => setD({ ...d, instructions: h.instructions })}>Put this text back in the editor</button> : null}
            </div>
          ))}
        </details>
      ) : null}
    </Card>
    <ForgeAiTuning slug={skill.slug} instructions={d.instructions} changed={changed} canEdit={canEdit} />
    </>
  );
}

/** Automation › ForgeAI skills: what ForgeAI is told for each kind of incident and task, with results. */
export function ForgeAiSkills() {
  const [data, setData] = useState<Data | null>(null);
  const [slug, setSlug] = useState("");
  const [error, setError] = useState("");
  const load = () => getJson<Data>("/api/v1/forgeai/skills").then((d) => (setData(d), setSlug((s) => s || d.skills[0]?.slug || ""))).catch((e: Error) => setError(e.message));
  useEffect(() => void load(), []);
  const add = () =>
    postJson<Skill>("/api/v1/forgeai/skills", { name: "New skill", task: "incident", match_alerts: ["*example*"], instructions: "Describe what ForgeAI should look at for this kind of incident and what to check first." })
      .then((s) => (setSlug(s.slug), load())).catch((e: Error) => setError(e.message));
  const current = data?.skills.find((s) => s.slug === slug);
  return (
    <>
      <Card title="ForgeAI skills" actions={data?.can_edit ? <button className="btn primary" type="button" onClick={add}>New skill</button> : null}>
        <span className="small faint">
          A skill tells ForgeAI what to look at for one kind of incident. For each incident the first skill (by order) whose alarm names and
          asset types match is used; “Any incident” catches the rest. Operators can ask again with another skill on the incident page.
        </span>
        {error ? <span className="small" role="alert" style={{ color: "var(--crit)" }}>{error}</span> : null}
        {data ? Object.entries(data.tasks).map(([task, label]) => (
          <div key={task} className="cell-stack" style={{ gap: 4 }}>
            <strong className="small faint" style={{ textTransform: "uppercase", letterSpacing: ".05em" }}>{label}</strong>
            {data.skills.filter((s) => s.task === task).map((s) => (
              <button key={s.slug} type="button" className="row" aria-pressed={s.slug === slug} onClick={() => setSlug(s.slug)}
                style={{ justifyContent: "space-between", background: s.slug === slug ? "var(--s2)" : "transparent", border: "1px solid var(--border)", borderRadius: 8, padding: "6px 10px", color: "var(--text)", cursor: "pointer", textAlign: "left" }}>
                <span>{s.name}{s.enabled ? "" : " (off)"} <span className="small faint">{[...s.match_alerts, ...s.match_types].join(", ")}</span></span>
                <StatLine s={s.stats} />
              </button>
            ))}
          </div>
        )) : <span className="muted">Loading…</span>}
      </Card>
      {current && data ? <Editor skill={current} canEdit={data.can_edit} onSaved={() => void load()} /> : null}
    </>
  );
}
