import { useEffect, useState } from "react";
import { getJson, postJson } from "../lib/api";
import { clock } from "../lib/format";
import { CallLog, type Call } from "./ForgeAiCard";
import { Card, Chip } from "./ui";

type Status = {
  enabled: boolean; url: string; model: string; timeout_s: number; reachable: boolean | null; loaded_model: string; why: string;
  week: { calls: number; used: number; rejected: number; errors: number; avg_ms: number; thumbs_up: number; thumbs_down: number };
  last: Call[];
};

/** Settings › ForgeRCA / ForgeAI: is the model there and answering, how the last week went, a test run. */
export function ForgeAiStatus() {
  const [s, setS] = useState<Status | null>(null);
  const [trial, setTrial] = useState<Call | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const load = () => getJson<Status>("/api/v1/forgeai/status").then((d) => setS(d && d.week ? d : null)).catch((e: Error) => setError(e.message));
  useEffect(() => void load(), []);
  const test = async () => {
    setBusy(true);
    setError("");
    setTrial(null);
    try {
      const { job } = await postJson<{ job: number }>("/api/v1/forgeai/trial", {});
      for (let i = 0; i < 120; i++) {
        const st = await getJson<{ status: string; error: string; call_id: number | null }>(`/api/v1/forgeai/jobs/${job}`);
        if (st.status === "error") throw new Error(st.error || "The test failed.");
        if (st.status === "done" && st.call_id) {
          setTrial(await getJson<Call>(`/api/v1/forgeai/calls/${st.call_id}`));
          break;
        }
        await new Promise((r) => setTimeout(r, 2500));
      }
      void load();
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setBusy(false);
    }
  };
  const w = s?.week;
  return (
    <Card title="ForgeAI right now" actions={<button className="btn" type="button" onClick={test} disabled={busy || !s?.enabled}>{busy ? "Asking the model…" : "Test on the newest incident"}</button>}>
      {s ? (
        <div className="row" style={{ gap: 8, flexWrap: "wrap" }}>
          <Chip tone={s.enabled ? "ok" : "off"}>{s.enabled ? "On" : "Off"}</Chip>
          {s.enabled ? <Chip tone={s.reachable ? "ok" : "crit"}>{s.reachable ? `Model loaded: ${s.loaded_model || s.model}` : "Model not answering"}</Chip> : null}
          <span className="small faint">{s.url || "no URL"} · timeout {s.timeout_s} s</span>
        </div>
      ) : null}
      {s?.why ? <span className="small" style={{ color: "var(--crit)" }}>{s.why}</span> : null}
      {w ? (
        <span className="small">
          Last 7 days: {w.calls} questions · {w.used} used · {w.rejected} rejected by the checks · {w.errors} without an answer ·
          average {(w.avg_ms / 1000).toFixed(1)} s · 👍 {w.thumbs_up} · 👎 {w.thumbs_down}
        </span>
      ) : null}
      <span className="small faint">
        For each incident ForgeAI asks the model once, with the skill that matches the alarm, and checks the answer: no number or IP that is
        not in the facts, no shell commands. Rejected answers are never shown to operators; the reason is kept below.
      </span>
      {error ? <span className="small" role="alert" style={{ color: "var(--crit)" }}>{error}</span> : null}
      {trial ? (
        <div className="cell-stack" style={{ gap: 6 }}>
          <strong>Test answer</strong>
          <pre className="mono small" style={{ whiteSpace: "pre-wrap", margin: 0 }}>{JSON.stringify(trial.answer, null, 2)}</pre>
          <CallLog calls={[trial]} />
        </div>
      ) : null}
      {s?.last.length ? (
        <>
          <strong className="small">Last questions</strong>
          <table className="data">
            <thead><tr><th style={{ width: 90 }}>When</th><th>Task · skill</th><th style={{ width: 90 }}>Time</th><th style={{ width: 100 }}>Outcome</th></tr></thead>
            <tbody>
              {s.last.map((c) => (
                <tr key={c.id}>
                  <td className="small muted">{clock(c.at)}</td>
                  <td className="small">{c.task} · {c.skill}{c.reason ? <div className="faint">{c.reason}</div> : null}</td>
                  <td className="small">{(c.duration_ms / 1000).toFixed(1)} s</td>
                  <td><Chip tone={c.status === "used" ? "ok" : c.status === "error" ? "crit" : c.status === "rejected" ? "warn" : "off"}>{c.status}</Chip></td>
                </tr>
              ))}
            </tbody>
          </table>
        </>
      ) : null}
    </Card>
  );
}
