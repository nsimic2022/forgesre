import { ChangesCard } from "../components/ChangesCard";
import { AskForgeAi } from "../components/AskForgeAi";
import { ForgeAiCard } from "../components/ForgeAiCard";
import { IncidentLinks } from "../components/IncidentLinks";
import { FollowUpActions, MajorIncidentCard, SlaChips } from "../components/IncidentProcess";
import { IncidentMetrics, IncidentStory, SimilarBefore, SyslogCard, WhoToCall } from "../components/IncidentPanels";
import { PostmortemPanel } from "../components/PostmortemPanel";
import { useCallback, useEffect, useState } from "react";
import { Link, useParams } from "react-router-dom";
import { ReviewCard } from "../components/ReviewCard";
import { SendReport } from "../components/SendReport";
import { Bar, Card, Chip } from "../components/ui";
import { getJson, postJson, type IncidentView } from "../lib/api";
import { clock, duration, severityLabel, severityTone, tierLabel, shortNumber } from "../lib/format";

const ACTIVE = ["OPEN", "INVESTIGATING", "ESCALATED", "ACK", "ACKNOWLEDGED"];

/** Incident detail: facts and hypotheses from ForgeRCA, the ForgeAI opinion, playbook
 *  checklist, asset card, notifications and timeline. Refreshes every 15 s. */
export function IncidentDetailPage() {
  const { number = "" } = useParams();
  const [data, setData] = useState<IncidentView | null>(null);
  const [error, setError] = useState("");
  const [busy, setBusy] = useState("");
  const [note, setNote] = useState("");
  const [postmortem, setPostmortem] = useState(false);
  const [mail, setMail] = useState(false);

  const load = useCallback(
    () =>
      getJson<IncidentView>(`/api/v1/incidents/${encodeURIComponent(number)}/view`)
        .then((next) => (setData(next), setError("")))
        .catch((err: Error) => setError(err.message)),
    [number],
  );
  useEffect(() => {
    load();
    const timer = window.setInterval(load, 15_000);
    return () => window.clearInterval(timer);
  }, [load]);

  const act = async (label: string, run: () => Promise<unknown>) => {
    setBusy(label);
    try {
      await run();
      await load();
    } catch (err) {
      setError((err as Error).message);
    } finally {
      setBusy("");
    }
  };
  const setStatus = (status: string) => act(status, () => postJson(`/api/v1/incidents/${number}/status`, { status }));

  if (!data) {
    return error ? <div className="banner error" role="alert">{error}</div> : <p className="muted">Loading…</p>;
  }
  const tone = severityTone(data.severity);
  const open = ACTIVE.includes(data.status.toUpperCase());
  const rca = data.forgerca;
  return (
    <>
      <header className="page-head">
        <div className="cell-stack" style={{ gap: 6 }}>
          <span className="small faint">
            <Link to="/incidents">Incidents</Link> / <span className="mono" title={data.number}>{shortNumber(data.number)}</span>
          </span>
          <div className="row">
            <Chip tone={tone}>{severityLabel(data.severity)}</Chip>
            <h1>{data.title}</h1>
          </div>
          <span className="muted">
            {[data.source, data.asset?.hostname, data.asset?.type, data.started_at ? `opened ${clock(data.started_at)} (${duration(data.started_at)} ago)` : ""].filter(Boolean).join(" · ")}
          </span>
        </div>
        <div className="row">
          <button className="btn" type="button" onClick={() => setPostmortem(!postmortem)} aria-expanded={postmortem}>Postmortem</button>
          {data.can.work ? <button className="btn" type="button" onClick={() => setMail(!mail)} aria-expanded={mail}>Send report</button> : null}
          {data.ticket ? (
            <a className="btn" href={data.ticket.url} target="_blank" rel="noopener noreferrer">Ticket {data.ticket.ref.replace("glpi:", "#")}{data.ticket.status ? ` · ${data.ticket.status}` : ""}</a>
          ) : data.can.work && data.can_ticket ? (
            <button className="btn" type="button" disabled={Boolean(busy)}
              onClick={() => void postJson(`/api/v1/incidents/${encodeURIComponent(data.number)}/ticket`, {}).then(() => setTimeout(() => void load(), 4000)).catch((e: Error) => setError(e.message))}>Open ticket</button>
          ) : null}
        {data.can.work && open ? (
          <>
            {!data.ack_at ? (
              <button className="btn primary" disabled={Boolean(busy)} onClick={() => setStatus("INVESTIGATING")}>Acknowledge</button>
            ) : null}
            <button className="btn" disabled={Boolean(busy)} onClick={() => setStatus("ESCALATED")}>Escalate</button>
            <button className="btn" disabled={Boolean(busy)} onClick={() => setStatus("RESOLVED")}>Resolve</button>
          </>
        ) : null}
        </div>
      </header>
      {postmortem ? <PostmortemPanel number={data.number} onClose={() => setPostmortem(false)} /> : null}
      {mail ? <SendReport number={data.number} onClose={() => setMail(false)} /> : null}
      {error ? <div className="banner error" role="alert">{error}</div> : null}
      <div className="banner" style={{ background: open ? "var(--high-bg)" : "var(--ok-bg)", border: "1px solid var(--border)" }}>
        <strong>{data.status}</strong>
        {data.ack_at ? ` · acknowledged by ${data.ack_by} at ${clock(data.ack_at)}` : open ? " · not acknowledged" : ""}
        {data.resolved_at ? ` · resolved by ${data.resolved_by || "system"} at ${clock(data.resolved_at)}` : ""}
        {` · escalation ${tierLabel(data.level)}`}
        {data.sla ? <span style={{ marginLeft: 10 }}><SlaChips sla={data.sla} /></span> : null}
      </div>
      {data.major ? <MajorIncidentCard number={data.number} major={data.major} canWork={data.can.work} onChanged={load} /> : null}
      <div className="cols">
        <div style={{ flex: "999 1 620px", display: "flex", flexDirection: "column", gap: 20, minWidth: 0 }}>
          <Card
            title="ForgeRCA · facts and hypotheses"
            actions={
              <>
                <Chip tone="ok">Rules · always on</Chip>
                {data.can.investigate ? (
                  <button className="btn" disabled={Boolean(busy)} onClick={() => act("rca", () => postJson(`/api/v1/incidents/${number}/investigate`, {}))}>
                    {busy === "rca" ? "Queued…" : "Run again"}
                  </button>
                ) : null}
              </>
            }
          >
            {rca ? (
              <>
                {rca.cause ? <p style={{ margin: 0, fontWeight: 600 }}>{rca.cause}</p> : null}
                <h3 className="small faint" style={{ margin: 0, textTransform: "uppercase", letterSpacing: ".05em" }}>Facts collected</h3>
                <ul style={{ margin: 0, paddingLeft: 18, display: "flex", flexDirection: "column", gap: 4 }}>
                  {rca.facts.map((f) => (
                    <li key={f.id}>{f.text}</li>
                  ))}
                </ul>
                {rca.hypotheses.length ? (
                  <>
                    <h3 className="small faint" style={{ margin: 0, textTransform: "uppercase", letterSpacing: ".05em" }}>Hypotheses</h3>
                    {rca.hypotheses.map((h) => (
                      <div key={h.id} style={{ display: "grid", gridTemplateColumns: "minmax(0,1fr) 140px 52px", gap: 12, alignItems: "center" }}>
                        <span>{h.summary}</span>
                        <Bar percent={h.confidence * 100} tone={h.rank === 1 ? "info" : "off"} />
                        <span className="small muted" style={{ textAlign: "right" }}>{Math.round(h.confidence * 100)}%</span>
                      </div>
                    ))}
                  </>
                ) : null}
                {rca.actions.length ? (
                  <>
                    <h3 className="small faint" style={{ margin: 0, textTransform: "uppercase", letterSpacing: ".05em" }}>What to do next</h3>
                    <ol style={{ margin: 0, paddingLeft: 18, display: "flex", flexDirection: "column", gap: 4 }}>
                      {rca.actions.map((a, i) => <li key={i}>{a}</li>)}
                    </ol>
                  </>
                ) : null}
                {rca.limitations.length ? (
                  <details>
                    <summary className="small faint" style={{ cursor: "pointer" }}>What ForgeRCA could not check ({rca.limitations.length})</summary>
                    <ul className="small muted" style={{ margin: "6px 0 0", paddingLeft: 18 }}>{rca.limitations.map((l, i) => <li key={i}>{l}</li>)}</ul>
                  </details>
                ) : null}
              </>
            ) : (
              <span className="muted">No analysis yet.</span>
            )}
          </Card>
          <ForgeAiCard number={data.number} view={data.forgeai} canAsk={data.can.investigate} onChanged={load} />
          {data.can.investigate && data.forgeai.mode !== "off" ? (
            <details className="card" style={{ padding: "12px 16px" }}>
              <summary style={{ cursor: "pointer", fontWeight: 600 }}>Ask ForgeAI · compare skills, your own question</summary>
              <div style={{ paddingTop: 12 }}><AskForgeAi number={data.number} /></div>
            </details>
          ) : null}
          {data.asset ? <IncidentMetrics assetId={data.asset.asset_id} number={data.number} /> : null}
          {data.events?.length ? <IncidentStory events={data.events} /> : null}
          {data.asset && data.can.investigate ? <SyslogCard number={data.number} /> : null}
          {data.evidence.length ? (
            <Card title="Evidence">
              <ul style={{ margin: 0, padding: 0, listStyle: "none" }}>
                {data.evidence.map((ev) => (
                  <li key={ev.id || ev.title} style={{ padding: "8px 0", borderTop: "1px solid var(--border)" }}>
                    <div className="row" style={{ justifyContent: "space-between" }}>
                      <span>{ev.title}</span>
                      <Chip tone="off">{ev.source}</Chip>
                    </div>
                    {ev.query ? <code className="mono faint">{ev.query}</code> : null}
                  </li>
                ))}
              </ul>
            </Card>
          ) : null}
        </div>
        <div style={{ flex: "1 1 340px", display: "flex", flexDirection: "column", gap: 20, minWidth: 0 }}>
          <WhoToCall people={data.who_to_call ?? []} />
          <ChangesCard changes={data.changes ?? []} />
          <SimilarBefore rows={data.similar ?? []} />
          <IncidentLinks number={data.number} links={data.links} canWork={data.can.work} canQuiet={Boolean(data.can.quiet)} hasAsset={Boolean(data.asset)} onChanged={load} />
          <ReviewCard number={data.number} review={data.review} canEdit={data.can.work} hasAi={Boolean(data.forgeai.text)} onSaved={load} />
          <FollowUpActions number={data.number} actions={data.actions ?? []} canWork={data.can.work} onChanged={load} />
          {data.asset ? (
            <Card title="Asset">
              <Link to={`/inventory?q=${encodeURIComponent(data.asset.hostname)}`} style={{ fontWeight: 700, fontSize: 15 }}>{data.asset.hostname}</Link>
              <dl style={{ margin: 0, display: "grid", gridTemplateColumns: "110px minmax(0,1fr)", gap: "6px 12px", fontSize: 13 }}>
                <dt className="faint">IP</dt><dd style={{ margin: 0 }} className="mono">{data.asset.ip || "—"}</dd>
                <dt className="faint">Type</dt><dd style={{ margin: 0 }}>{data.asset.type}</dd>
                <dt className="faint">Environment</dt><dd style={{ margin: 0 }}>{data.asset.environment || "—"}</dd>
                <dt className="faint">Owner</dt><dd style={{ margin: 0 }}>{data.asset.owner || "—"}</dd>
                <dt className="faint">Contact</dt><dd style={{ margin: 0 }}>{[data.asset.contact_name, data.asset.owner_phone, data.asset.owner_email].filter(Boolean).join(" · ") || "—"}</dd>
                <dt className="faint">Sources</dt><dd style={{ margin: 0 }}>{[data.asset.glpi_id && `GLPI #${data.asset.glpi_id}`, data.asset.zabbix_hostid && `Zabbix ${data.asset.zabbix_hostid}`].filter(Boolean).join(" · ") || "—"}</dd>
              </dl>
              {data.asset.notes ? <p className="small muted" style={{ margin: 0 }}>{data.asset.notes}</p> : null}
            </Card>
          ) : null}
          {data.playbook ? (
            <Card title={`Playbook · ${data.playbook.name}`} actions={<span className="small faint">{data.playbook.done.length} / {data.playbook.steps.length}</span>}>
              <Bar percent={data.playbook.steps.length ? (data.playbook.done.length / data.playbook.steps.length) * 100 : 0} tone="info" />
              {data.playbook.steps.map((step, index) => {
                const done = data.playbook!.done.includes(index);
                return (
                  <label key={index} className="row" style={{ alignItems: "flex-start", flexWrap: "nowrap" }}>
                    <input
                      type="checkbox"
                      checked={done}
                      disabled={!data.can.work || Boolean(busy)}
                      onChange={() => act("step", () => postJson(`/api/v1/incidents/${number}/checklist`, { step: index, done: !done }))}
                      style={{ marginTop: 3, accentColor: "var(--accent)" }}
                    />
                    <span style={done ? { color: "var(--faint)", textDecoration: "line-through" } : undefined}>{step}</span>
                  </label>
                );
              })}
            </Card>
          ) : null}
          <Card title="Notifications">
            {data.notifications.length === 0 ? <span className="muted">Nothing sent yet.</span> : null}
            <ul style={{ margin: 0, padding: 0, listStyle: "none" }}>
              {data.notifications.map((n) => (
                <li key={n.id} style={{ padding: "8px 0", borderTop: "1px solid var(--border)", display: "flex", justifyContent: "space-between", gap: 10 }}>
                  <div className="cell-stack">
                    <span>{n.channel} → {n.target}</span>
                    <span className="small faint">{n.step_key} · {clock(n.created_at)}</span>
                  </div>
                  <Chip tone={n.status === "sent" ? "ok" : n.error ? "crit" : "off"}>{n.status}</Chip>
                </li>
              ))}
            </ul>
          </Card>
          <Card title="Notes">
            {data.notes.map((n) => (
              <div key={n.id} className="cell-stack" style={{ borderTop: "1px solid var(--border)", paddingTop: 8 }}>
                <span>{n.body}</span>
                <span className="small faint">{n.actor} · {clock(n.at)}</span>
              </div>
            ))}
            {data.can.work ? (
              <form
                className="cell-stack"
                onSubmit={(e) => {
                  e.preventDefault();
                  if (note.trim()) act("note", () => postJson(`/api/v1/incidents/${number}/notes`, { body: note }).then(() => setNote("")));
                }}
              >
                <label className="small faint" htmlFor="note">Add a note</label>
                <textarea id="note" value={note} onChange={(e) => setNote(e.target.value)} rows={3} className="search" style={{ padding: 10, minHeight: 70 }} />
                <div><button className="btn" type="submit" disabled={!note.trim() || Boolean(busy)}>Save note</button></div>
              </form>
            ) : null}
          </Card>
        </div>
      </div>
    </>
  );
}
