import { useEffect, useState } from "react";
import { Link } from "react-router-dom";
import { AskForgeAi } from "../components/AskForgeAi";
import type { Skill } from "../components/ForgeAiSkills";
import { RATING_LABEL, RATING_TONE } from "../components/ReviewCard";
import { Bar, Card, Chip } from "../components/ui";
import { getJson, type Accuracy, type InvestigationsPage as Page } from "../lib/api";
import { clock, shortNumber } from "../lib/format";

const PAGE = 50;

function AccuracyRow({ label, a }: { label: string; a: Accuracy }) {
  return (
    <div className="cell-stack" style={{ gap: 6 }}>
      <div className="row" style={{ justifyContent: "space-between" }}>
        <span>{label}</span>
        <span style={{ fontWeight: 600 }}>{a.score === null ? "—" : `${a.score}%`}</span>
      </div>
      <Bar percent={a.score ?? 0} tone="info" />
      <span className="small faint">{a.rated ? `${a.correct} correct · ${a.partly} partly · ${a.wrong} wrong · ${a.rated} rated` : "No reviews yet"}</span>
    </div>
  );
}

export function InvestigationsPage() {
  const [days, setDays] = useState(90);
  const [query, setQuery] = useState("");
  const [onlyAi, setOnlyAi] = useState(false);
  const [onlyReviewed, setOnlyReviewed] = useState(false);
  const [offset, setOffset] = useState(0);
  const [data, setData] = useState<Page | null>(null);
  const [error, setError] = useState("");
  const [picked, setPicked] = useState("");
  const [typed, setTyped] = useState("");

  useEffect(() => {
    const params = new URLSearchParams({ days: String(days), q: query, only_ai: String(onlyAi), only_reviewed: String(onlyReviewed), limit: String(PAGE), offset: String(offset) });
    const timer = window.setTimeout(() => {
      getJson<Page>(`/api/v1/investigations-page?${params}`).then((d) => (setData(d), setError(""))).catch((e: Error) => setError(e.message));
    }, query ? 250 : 0);
    return () => window.clearTimeout(timer);
  }, [days, query, onlyAi, onlyReviewed, offset]);

  const reset = (fn: () => void) => (fn(), setOffset(0));
  return (
    <>
      <header className="page-head">
        <div>
          <h1>Investigations</h1>
          <span className="muted">Every resolved incident with its confirmed cause and how well ForgeRCA and ForgeAI did</span>
        </div>
      </header>
      {error ? <div className="banner error" role="alert">{error}</div> : null}
      <div className="cols">
        <div style={{ flex: "999 1 640px", minWidth: 0 }}>
          <Card>
            <div className="row">
              <input className="search" type="search" aria-label="Search investigations" placeholder="Host, alert, cause…" value={query} onChange={(e) => reset(() => setQuery(e.target.value))} />
              {[30, 90, 365].map((d) => (
                <button key={d} className="btn" aria-pressed={days === d} onClick={() => reset(() => setDays(d))}>Last {d} days</button>
              ))}
              <button className="btn" aria-pressed={onlyAi} onClick={() => reset(() => setOnlyAi(!onlyAi))}>Only with ForgeAI</button>
              <button className="btn" aria-pressed={onlyReviewed} onClick={() => reset(() => setOnlyReviewed(!onlyReviewed))}>Only confirmed cause</button>
            </div>
            <div className="table-wrap">
              <table className="data" style={{ minWidth: 1110 }}>
                <thead>
                  <tr>
                    <th style={{ width: 110 }}>Incident</th>
                    <th style={{ width: 90 }}>Date</th>
                    <th style={{ width: 130 }}>Host</th>
                    <th style={{ width: 150 }}>Problem</th>
                    <th style={{ width: 80 }}>Duration</th>
                    <th>Confirmed cause</th>
                    <th style={{ width: 160 }}>ForgeRCA said</th>
                    <th style={{ width: 96 }}>ForgeRCA</th>
                    <th style={{ width: 96 }}>ForgeAI</th>
                    <th style={{ width: 70 }}></th>
                  </tr>
                </thead>
                <tbody>
                  {(data?.rows ?? []).map((r) => (
                    <tr key={r.number}>
                      <td><Link className="mono" to={`/incidents/${encodeURIComponent(r.number)}`} title={r.number}>{shortNumber(r.number)}</Link></td>
                      <td className="small muted">{clock(r.started_at)}</td>
                      <td style={{ fontWeight: 600 }}>{r.host || "—"}</td>
                      <td>{r.title}</td>
                      <td className="small muted">{r.minutes === null ? "—" : r.minutes < 60 ? `${r.minutes} min` : `${Math.floor(r.minutes / 60)} h ${r.minutes % 60} min`}</td>
                      <td>{r.root_cause || <span className="faint">not reviewed</span>}</td>
                      <td className="small muted">{r.forgerca || "—"}</td>
                      <td><Chip tone={RATING_TONE[r.rca_rating]}>{RATING_LABEL[r.rca_rating]}</Chip></td>
                      <td>{r.forgeai ? <Chip tone={RATING_TONE[r.ai_rating]}>{RATING_LABEL[r.ai_rating]}</Chip> : <span className="faint">—</span>}</td>
                      <td>
                        <button className="btn" type="button" aria-pressed={picked === r.number} title="Ask ForgeAI about this incident below"
                          onClick={() => (setPicked(r.number), setTyped(r.number), window.setTimeout(() => document.getElementById("ask-forgeai")?.scrollIntoView({ behavior: "smooth" }), 50))}>Ask</button>
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
              {data && data.rows.length === 0 ? <div className="empty">No resolved incidents match.</div> : null}
            </div>
            {data && data.total > PAGE ? (
              <div className="row" style={{ justifyContent: "space-between" }}>
                <span className="small faint">{offset + 1}–{Math.min(offset + PAGE, data.total)} of {data.total}</span>
                <div className="row">
                  <button className="btn" disabled={offset === 0} onClick={() => setOffset(Math.max(0, offset - PAGE))}>Previous</button>
                  <button className="btn" disabled={offset + PAGE >= data.total} onClick={() => setOffset(offset + PAGE)}>Next</button>
                </div>
              </div>
            ) : null}
            <span className="small faint">Analysts confirm the cause and rate each analysis on the incident page.</span>
          </Card>
        </div>
        <div style={{ flex: "1 1 320px", minWidth: 0, display: "flex", flexDirection: "column", gap: 20 }}>
          {data ? (
            <Card title={`Accuracy · last ${data.days} days`} actions={<span className="small faint">{data.reviewed} of {data.resolved} reviewed</span>}>
              <AccuracyRow label="ForgeRCA top hypothesis" a={data.accuracy.forgerca} />
              <AccuracyRow label="ForgeAI opinion" a={data.accuracy.forgeai} />
              <span className="small faint">Partly right counts half.</span>
            </Card>
          ) : null}
          {data ? (
            <Card title="Recurring causes">
              {data.recurring.length === 0 ? <span className="muted">No cause confirmed twice yet.</span> : null}
              {data.recurring.map((r) => (
                <div key={r.cause} className="row" style={{ justifyContent: "space-between", borderTop: "1px solid var(--border)", paddingTop: 8 }}>
                  <div className="cell-stack"><span>{r.cause}</span><span className="small faint">{r.hosts.join(", ")}</span></div>
                  <Chip tone="high">{r.count}×</Chip>
                </div>
              ))}
            </Card>
          ) : null}
          <SkillUsefulness />
        </div>
      </div>
      <section id="ask-forgeai" className="cell-stack" style={{ gap: 12, marginTop: 20 }}>
        <div className="row" style={{ justifyContent: "space-between", flexWrap: "wrap", gap: 10 }}>
          <div className="cell-stack">
            <h2 style={{ margin: 0 }}>Ask ForgeAI</h2>
            <span className="small muted">See what it was asked, ask again with another skill, or ask your own question. Skills and model settings stay in Automation and Settings.</span>
          </div>
          <form className="row" onSubmit={(e) => (e.preventDefault(), setPicked(typed.trim()))}>
            <input className="search" aria-label="Incident number" placeholder="INC-… (any incident)" value={typed} onChange={(e) => setTyped(e.target.value)} style={{ width: 240, minWidth: 0 }} />
            <button className="btn" type="submit" disabled={!typed.trim()}>Open</button>
          </form>
        </div>
        {picked ? <AskForgeAi number={picked} /> : <Card><span className="muted">Press <strong>Ask</strong> on a row above, or type an incident number (open ones too).</span></Card>}
      </section>
    </>
  );
}

/** Which ForgeAI skills help, from the last 30 days of answers and 👍 / 👎. */
function SkillUsefulness() {
  const [skills, setSkills] = useState<Skill[] | null>(null);
  useEffect(() => void getJson<{ skills: Skill[] }>("/api/v1/forgeai/skills").then((d) => setSkills(d.skills.filter((s) => s.stats?.calls))).catch(() => setSkills([])), []);
  if (!skills) return null;
  return (
    <Card title="ForgeAI skills · 30 days" actions={<Link className="small" to="/automation">Edit skills</Link>}>
      {skills.length === 0 ? <span className="muted">ForgeAI has not answered anything in the last 30 days.</span> : null}
      {skills.map((s) => (
        <div key={s.slug} className="cell-stack" style={{ gap: 2, borderTop: "1px solid var(--border)", paddingTop: 8 }}>
          <div className="row" style={{ justifyContent: "space-between" }}>
            <span>{s.name}</span>
            <span className="small">👍 {s.stats.up ?? 0} · 👎 {s.stats.down ?? 0}</span>
          </div>
          <span className="small faint">{s.stats.calls} questions · {s.stats.used} used · {s.stats.rejected} rejected by the checks · {s.stats.avg_s} s</span>
        </div>
      ))}
    </Card>
  );
}
