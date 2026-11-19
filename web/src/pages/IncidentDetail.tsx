import { useCallback, useEffect, useState } from "react";
import { Link, useParams } from "react-router-dom";
import { ReviewCard } from "../components/ReviewCard";
import { Bar, Card, Chip } from "../components/ui";
import { getJson, postJson, type IncidentView } from "../lib/api";
import { clock, duration, severityLabel, severityTone } from "../lib/format";

const ACTIVE = ["OPEN", "INVESTIGATING", "ESCALATED", "ACK", "ACKNOWLEDGED"];

/** Incident detail: facts and hypotheses from ForgeRCA, the ForgeAI opinion, playbook
 *  checklist, asset card, notifications and timeline. Refreshes every 15 s. */
export function IncidentDetailPage() {
  const { number = "" } = useParams();
  const [data, setData] = useState<IncidentView | null>(null);
  const [error, setError] = useState("");
  const [busy, setBusy] = useState("");
  const [note, setNote] = useState("");

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
            <Link to="/incidents">Incidents</Link> / <span className="mono">{data.number}</span>
          </span>
          <div className="row">
            <Chip tone={tone}>{severityLabel(data.severity)}</Chip>
            <h1>{data.title}</h1>
          </div>
          <span className="muted">
            {[data.source, data.asset?.hostname, data.asset?.type, data.started_at ? `opened ${clock(data.started_at)} (${duration(data.started_at)} ago)` : ""].filter(Boolean).join(" · ")}
          </span>
        </div>
        {data.can.work && open ? (
          <div className="row">
            {!data.ack_at ? (
              <button className="btn primary" disabled={Boolean(busy)} onClick={() => setStatus("INVESTIGATING")}>Acknowledge</button>
            ) : null}
            <button className="btn" disabled={Boolean(busy)} onClick={() => setStatus("ESCALATED")}>Escalate</button>
            <button className="btn" disabled={Boolean(busy)} onClick={() => setStatus("RESOLVED")}>Resolve</button>
          </div>
        ) : null}
      </header>
      {error ? <div className="banner error" role="alert">{error}</div> : null}
      <div className="banner" style={{ background: open ? "var(--high-bg)" : "var(--ok-bg)", border: "1px solid var(--border)" }}>
        <strong>{data.status}</strong>
        {data.ack_at ? ` · acknowledged by ${data.ack_by} at ${clock(data.ack_at)}` : open ? " · not acknowledged" : ""}
        {data.resolved_at ? ` · resolved by ${data.resolved_by || "system"} at ${clock(data.resolved_at)}` : ""}
        {` · escalation ${data.level}`}
      </div>
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
                {rca.actions.length ? <p style={{ margin: 0 }}><span className="faint">Suggested: </span>{rca.actions.join(" ")}</p> : null}
              </>
            ) : (
              <span className="muted">No analysis yet.</span>
            )}
          </Card>
          <Card title="ForgeAI · opinion" actions={<Chip tone={data.forgeai.mode === "off" ? "off" : "info"}>{data.forgeai.mode === "off" ? "Off" : data.forgeai.mode === "external" ? "External model" : "Local model"}</Chip>}>
            {data.forgeai.text ? (
              <>
                <p style={{ margin: 0, lineHeight: 1.6 }}>{data.forgeai.text}</p>
                {data.forgeai.action ? <p style={{ margin: 0 }}><span className="faint">Next step: </span>{data.forgeai.action}</p> : null}
                <span className="small faint">Advisory opinion. Facts above come from ForgeRCA.{data.forgeai.model ? ` Model: ${data.forgeai.model}.` : ""}</span>
              </>
            ) : (
              <span className="muted">{data.forgeai.mode === "off" ? "ForgeAI is off in Settings." : "No ForgeAI opinion yet; it is written after ForgeRCA finishes."}</span>
            )}
          </Card>
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
          <ReviewCard number={data.number} review={data.review} canEdit={data.can.work} hasAi={Boolean(data.forgeai.text)} onSaved={load} />
          {data.asset ? (
            <Card title="Asset">
              <a href={`/assets?q=${encodeURIComponent(data.asset.hostname)}`} style={{ fontWeight: 700, fontSize: 15 }}>{data.asset.hostname}</a>
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
