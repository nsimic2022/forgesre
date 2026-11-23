import { useEffect, useState } from "react";
import { getJson, postJson } from "../lib/api";
import { clock } from "../lib/format";
import { Card, Chip } from "./ui";

type Case = { id: number; incident: string; title: string; cause: string; expect: string[] };
type Result = { incident: string; passed: boolean; why: string; cause: string; ms: number; skill: string };
type Run = { id: number; started_at: string | null; finished_at: string | null; model: string; skill: string; language: string; passed: number; total: number; results: Result[] };
type Golden = { cases: Case[]; runs: Run[]; running: boolean; can_edit: boolean };

/**
 * Automation › ForgeAI skills › Golden set: resolved incidents whose confirmed cause is right,
 * replayed against the model to compare models, skills and the answer language over time.
 */
export function GoldenSet() {
  const [data, setData] = useState<Golden | null>(null);
  const [number, setNumber] = useState("");
  const [expect, setExpect] = useState("");
  const [skill, setSkill] = useState("");
  const [error, setError] = useState("");
  const load = () => getJson<Golden>("/api/v1/forgeai/golden").then(setData).catch((e: Error) => setError(e.message));
  useEffect(() => void load(), []);
  useEffect(() => {
    if (!data?.running) return;
    const t = window.setInterval(load, 5000);
    return () => window.clearInterval(t);
  }, [data?.running]);
  const act = async (fn: () => Promise<unknown>) => {
    setError("");
    try {
      await fn();
      await load();
    } catch (e) {
      setError((e as Error).message);
    }
  };
  if (!data) return null;
  const cases = Array.isArray(data.cases) ? data.cases : [];
  const runs = Array.isArray(data.runs) ? data.runs : [];
  const last = runs[0];
  return (
    <Card title="Golden set" actions={<span className="small faint">{cases.length} incidents{data.running ? " · run in progress" : ""}</span>}>
      <span className="small faint">Resolved incidents with a confirmed cause. A run asks the model again for each one (trial, nothing changes on the incident) and passes it when the answer is safe and names the expected words.</span>
      {error ? <div className="banner error" role="alert">{error}</div> : null}
      {cases.length ? (
        <table className="data">
          <thead><tr><th>Incident</th><th>Confirmed cause</th><th>Expects</th>{last ? <th>Last run</th> : null}{data.can_edit ? <th /> : null}</tr></thead>
          <tbody>
            {cases.map((c) => {
              const res = last?.results?.find((r) => r.incident === c.incident.split("_")[0]);
              return (
                <tr key={c.id}>
                  <td className="mono small">{c.incident.split("_")[0]}</td>
                  <td className="small">{c.cause || c.title}</td>
                  <td className="small">{c.expect.join(", ")}</td>
                  {last ? <td>{res ? <span title={res.cause}><Chip tone={res.passed ? "ok" : "crit"}>{res.passed ? "pass" : "fail"}</Chip> <span className="small faint">{res.why}</span></span> : "–"}</td> : null}
                  {data.can_edit ? <td><button className="btn" type="button" aria-label={`Remove ${c.incident}`} onClick={() => void act(() => getJson(`/api/v1/forgeai/golden/${c.id}`, { method: "DELETE" }))}>Remove</button></td> : null}
                </tr>
              );
            })}
          </tbody>
        </table>
      ) : <span className="muted">No incident in the golden set yet.</span>}
      {data.can_edit ? (
        <div className="row" style={{ gap: 8, flexWrap: "wrap" }}>
          <form className="row" style={{ gap: 6 }} onSubmit={(e) => (e.preventDefault(), void act(() => postJson("/api/v1/forgeai/golden", { number, expect }).then(() => (setNumber(""), setExpect("")))))}>
            <input className="search" aria-label="Incident number" placeholder="INC-…" value={number} onChange={(e) => setNumber(e.target.value)} />
            <input className="search" aria-label="Expected words" placeholder="expected words, comma (optional)" value={expect} onChange={(e) => setExpect(e.target.value)} />
            <button className="btn" type="submit" disabled={!number.trim()}>Add</button>
          </form>
          <input className="search" aria-label="Skill to test" placeholder="skill slug (best match if empty)" value={skill} onChange={(e) => setSkill(e.target.value)} />
          <button className="btn primary" type="button" disabled={!cases.length || data.running} onClick={() => void act(() => postJson("/api/v1/forgeai/golden/run", { skill }))}>Run golden set</button>
        </div>
      ) : null}
      {runs.length ? (
        <table className="data" aria-label="Golden runs">
          <thead><tr><th>Run</th><th>Score</th><th>Model</th><th>Skill</th><th>Language</th><th>Finished</th></tr></thead>
          <tbody>
            {runs.map((r) => (
              <tr key={r.id}>
                <td className="mono small">#{r.id}</td>
                <td><Chip tone={r.total && r.passed === r.total ? "ok" : r.passed * 2 >= r.total ? "warn" : "crit"}>{r.passed}/{r.total}</Chip></td>
                <td className="small">{r.model || "–"}</td>
                <td className="small">{r.skill}</td>
                <td className="small">{r.language}</td>
                <td className="small">{r.finished_at ? clock(r.finished_at) : "running…"}</td>
              </tr>
            ))}
          </tbody>
        </table>
      ) : null}
    </Card>
  );
}
