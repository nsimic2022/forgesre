import { useEffect, useRef, useState } from "react";
import { getJson, postJson } from "../lib/api";
import { Card, Chip } from "./ui";

type Example = { incident: string; title: string; said: string; confirmed: string; rating: string; thumbs: number; overlap: number | null; call_id: number; skill_version: number };
type Report = {
  skill: string; task: string; version: number; days: number; calls: number; used: number; rejected: number; errors: number;
  thumbs_up: number; thumbs_down: number; ratings: Record<string, number>; correct_share: number | null;
  reject_reasons: { reason: string; count: number }[]; error_reasons: { reason: string; count: number }[];
  examples: Example[]; replay_candidates: number;
};
type ReplayRow = { incident: string; title?: string; confirmed?: string; status: string; reason: string; said?: string; overlap?: number | null; before?: string; before_overlap?: number | null };
type Replay = { asked: number; answered: number; rejected: number; errors: number; mean_overlap: number | null; unsaved_text: boolean; rows: ReplayRow[]; seconds: number };

const TONE: Record<string, "ok" | "warn" | "crit" | "off"> = { correct: "ok", partly: "warn", wrong: "crit" };
const pct = (v: number | null | undefined) => (v === null || v === undefined ? "—" : `${Math.round(v * 100)}%`);

/** How a skill did on real incidents (against the analysts' confirmed causes) and a replay of saved or
 *  unsaved instructions on the last confirmed incidents, as trials that never reach an incident. */
export function ForgeAiTuning({ slug, instructions, changed, canEdit }: { slug: string; instructions: string; changed: boolean; canEdit: boolean }) {
  const [report, setReport] = useState<Report | null>(null);
  const [replay, setReplay] = useState<Replay | null>(null);
  const [busy, setBusy] = useState("");
  const [error, setError] = useState("");
  const alive = useRef(true);
  useEffect(() => () => void (alive.current = false), []);
  useEffect(() => {
    setReport(null);
    setReplay(null);
    getJson<Report>(`/api/v1/forgeai/skills/${slug}/tuning`).then((r) => Array.isArray(r?.examples) && setReport(r)).catch((e: Error) => setError(e.message));
  }, [slug]);
  const run = async () => {
    setBusy("Asking the model on each confirmed incident, one at a time (a few minutes)…");
    setError("");
    setReplay(null);
    try {
      const { job } = await postJson<{ job: number }>(`/api/v1/forgeai/skills/${slug}/replay`, { limit: 10, instructions: changed ? instructions : null });
      for (let i = 0; i < 400 && alive.current; i++) {
        const st = await getJson<{ status: string; error: string; result?: Replay }>(`/api/v1/forgeai/jobs/${job}`);
        if (st.status === "error") throw new Error(st.error || "The replay failed.");
        if (st.status === "done" && st.result) return void setReplay(st.result);
        await new Promise((r) => setTimeout(r, 3000));
      }
    } catch (e) {
      if (alive.current) setError((e as Error).message);
    } finally {
      if (alive.current) setBusy("");
    }
  };
  if (!report) return error ? <Card title="Tuning"><span className="small" role="alert" style={{ color: "var(--crit)" }}>{error}</span></Card> : null;
  return (
    <Card title="Tuning" actions={<span className="small faint">last {report.days} days · v{report.version}</span>}>
      <span className="small muted">
        {report.calls} questions · {report.used} used · {report.rejected} rejected · {report.errors} no answer · 👍 {report.thumbs_up} · 👎 {report.thumbs_down}
        {Object.keys(report.ratings).length ? ` · analysts: ${Object.entries(report.ratings).map(([k, v]) => `${v} ${k}`).join(", ")} (${pct(report.correct_share)} correct)` : " · no analyst ratings yet"}
      </span>
      {report.reject_reasons.length ? (
        <div className="cell-stack" style={{ gap: 2 }}>
          <strong className="small">Why answers were rejected</strong>
          {report.reject_reasons.map((r) => <span key={r.reason} className="small muted">{r.count} × {r.reason}</span>)}
          <span className="small faint">A rule in the instructions often fixes these, e.g. “use only numbers that are in the facts”.</span>
        </div>
      ) : null}
      {report.examples.length ? (
        <div className="table-wrap">
          <table className="data">
            <thead><tr><th style={{ width: 90 }}>Incident</th><th>ForgeAI said</th><th>Analyst confirmed</th><th style={{ width: 90 }}>Rating</th></tr></thead>
            <tbody>
              {report.examples.map((e) => (
                <tr key={e.call_id}>
                  <td className="mono small">{e.incident}</td>
                  <td className="small">{e.said}</td>
                  <td className="small muted">{e.confirmed || <span className="faint">not reviewed</span>}</td>
                  <td>{e.rating ? <Chip tone={TONE[e.rating] ?? "off"}>{e.rating}</Chip> : <span className="faint">—</span>}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      ) : <span className="small faint">No used answers in this period yet.</span>}
      {report.task === "incident" && canEdit ? (
        <div className="row">
          <button className="btn" type="button" disabled={Boolean(busy) || report.replay_candidates === 0} onClick={() => void run()}>
            {changed ? "Replay the unsaved text" : "Replay"} on the last {Math.min(10, report.replay_candidates)} confirmed incidents
          </button>
          {report.replay_candidates === 0 ? <span className="small faint">Needs resolved incidents with a confirmed cause (the review card on the incident page).</span> : null}
        </div>
      ) : null}
      {busy ? <span className="small faint">{busy}</span> : null}
      {error ? <span className="small" role="alert" style={{ color: "var(--crit)" }}>{error}</span> : null}
      {replay ? (
        <div className="cell-stack" style={{ gap: 6, borderTop: "1px solid var(--border)", paddingTop: 8 }}>
          <strong className="small">
            Replay of the {replay.unsaved_text ? "unsaved" : "saved"} text: {replay.answered}/{replay.asked} answered, {replay.rejected} rejected, {replay.errors} no answer, {replay.seconds} s ·
            word overlap with confirmed causes {pct(replay.mean_overlap)}
          </strong>
          <span className="small faint">Word overlap only helps sort; read the answers. Every replay answer is a trial and is not shown on the incidents.</span>
          <div className="table-wrap">
            <table className="data">
              <thead><tr><th style={{ width: 90 }}>Incident</th><th>Confirmed</th><th>Before</th><th>Now</th></tr></thead>
              <tbody>
                {replay.rows.map((r) => (
                  <tr key={r.incident}>
                    <td className="mono small">{r.incident}</td>
                    <td className="small muted">{r.confirmed}</td>
                    <td className="small">{r.before || <span className="faint">—</span>} {r.before_overlap !== null && r.before_overlap !== undefined ? <span className="faint">({pct(r.before_overlap)})</span> : null}</td>
                    <td className="small">{r.status === "trial" ? <>{r.said} <span className="faint">({pct(r.overlap)})</span></> : <span style={{ color: "var(--crit)" }}>{r.status}: {r.reason}</span>}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </div>
      ) : null}
    </Card>
  );
}
