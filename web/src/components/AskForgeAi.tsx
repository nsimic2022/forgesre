import { useEffect, useState } from "react";
import { getJson, postJson } from "../lib/api";
import { clock } from "../lib/format";
import { useRoleRank } from "../lib/me";
import { CallLog, type Call } from "./ForgeAiCard";
import { IncidentChat } from "./ForgeAiChat";
import { Card, Chip } from "./ui";

type Cloud = { enabled: boolean; ready: boolean; why: string; provider: string; model: string; host: string; can_use: boolean };

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
      {a.verdict ? (
        <span>
          <Chip tone={a.verdict === "agree" ? "ok" : a.verdict === "partly" ? "warn" : "crit"}>{String(a.verdict)}</Chip> {String(a.why ?? "")}
        </span>
      ) : null}
      {list("problems").length ? <ul style={{ margin: 0, paddingLeft: 18 }}>{list("problems").map((c, i) => <li key={i} className="small">{c}</li>)}</ul> : null}
      {a.better_cause ? <span className="small"><span className="faint">Cause it would suggest: </span>{String(a.better_cause)}</span> : null}
      {list("not_in_facts").length ? (
        <span className="small muted">Not in the facts: {list("not_in_facts").join(" · ")}</span>
      ) : null}
    </div>
  );
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
 * What the automatic ForgeAI run asked and got for one incident, with a cloud second opinion for
 * Engineers. Shown next to an incident chat on Investigations › Ask ForgeAI.
 */
export function ForgeAiFound({ number }: { number: string }) {
  const [calls, setCalls] = useState<Call[] | null>(null);
  const [busy, setBusy] = useState("");
  const [error, setError] = useState("");
  const [cloud, setCloud] = useState<Cloud | null>(null);

  const load = () =>
    getJson<{ calls: Call[] }>(`/api/v1/incidents/${encodeURIComponent(number)}/forgeai`).then((d) => setCalls(d.calls)).catch((e: Error) => (setCalls([]), setError(e.message)));
  useEffect(() => void getJson<{ cloud?: Cloud }>("/api/v1/forgeai/status").then((s) => setCloud(s.cloud ?? null)).catch(() => setCloud(null)), []);
  useEffect(() => {
    setCalls(null);
    setError("");
    void load();
  }, [number]);

  const automatic = (calls ?? []).find((c) => c.task === "incident" && c.status !== "trial") ?? null;
  const cloudOk = Boolean(cloud?.enabled && cloud.ready && cloud.can_use);
  const critics = (id: number) => (calls ?? []).filter((c) => c.task === "critic" && (c.answer as { reviews_call?: number })?.reviews_call === id);
  const secondOpinion = async (id: number) => {
    setBusy(`critic-${id}`);
    setError("");
    try {
      const { job } = await postJson<{ job: number }>(`/api/v1/forgeai/calls/${id}/critique`, {});
      await waitForCall(job);
      await load();
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setBusy("");
    }
  };
  return (
        <Card title="What ForgeAI found on its own">
          {calls === null ? <span className="muted">Loading…</span> : automatic ? (
            <>
              <span className="small faint">
                <Chip tone={automatic.status === "used" ? "ok" : automatic.status === "rejected" ? "warn" : "crit"}>{automatic.status}</Chip>{" "}
                skill {automatic.skill} · {automatic.model || "no model"} · {(automatic.duration_ms / 1000).toFixed(1)} s · {clock(automatic.at)}
              </span>
              <AnswerView call={automatic} />
              {critics(automatic.id).map((c) => (
                <div key={c.id} className="cell-stack" style={{ gap: 4, borderLeft: "3px solid var(--warn)", paddingLeft: 10 }}>
                  <span className="small faint">Second opinion · {c.model} (cloud) · {clock(c.at)}</span>
                  <AnswerView call={c} />
                </div>
              ))}
              {cloudOk && automatic.status !== "rejected" && automatic.status !== "error" && !critics(automatic.id).length ? (
                <button className="btn" type="button" disabled={Boolean(busy)} onClick={() => void secondOpinion(automatic.id)}
                  title={`Sends this answer and its facts to ${cloud?.host ?? "the cloud provider"}`}>
                  {busy ? "Asking the cloud model…" : `Second opinion (${cloud?.provider ?? "cloud"})`}
                </button>
              ) : null}
              <CallLog calls={[automatic]} />
            </>
          ) : <span className="muted">ForgeAI did not answer this incident on its own (it was off, or the incident is older).</span>}
          {error ? <span className="small" role="alert" style={{ color: "var(--crit)" }}>{error}</span> : null}
        </Card>
  );
}

/** Incident page › Ask ForgeAI: what it found on its own next to your chat about this incident. */
export function AskForgeAi({ number }: { number: string }) {
  const rank = useRoleRank();
  return (
    <div className="cols">
      <div style={{ flex: "1 1 380px", minWidth: 0 }}><ForgeAiFound number={number} /></div>
      <div style={{ flex: "2 1 480px", minWidth: 0 }}>
        <Card title="Chat about this incident">
          {rank >= 1 ? <IncidentChat number={number} /> : rank < 0 ? <span className="muted">Loading…</span> : <span className="small faint">Analysts and up can chat with ForgeAI.</span>}
        </Card>
      </div>
    </div>
  );
}
