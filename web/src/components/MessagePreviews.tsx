import { useEffect, useState, type FormEvent } from "react";
import { getJson } from "../lib/api";
import { Card, Chip } from "./ui";

type Mail = { subject: string; text: string; html: string };
type ChatEvent = { text: string; payloads: Record<string, unknown> };
export type Previews = {
  incident: string | null;
  public_url: string;
  note: string;
  alert_email: Mail;
  incident_report: Mail;
  chat: { events: Record<string, ChatEvent>; channels: Record<string, string>; active: string[] };
};

type Tab = "alert" | "chat" | "report";
const TABS: [Tab, string][] = [["alert", "Alert mail"], ["chat", "Slack, Teams, Discord"], ["report", "Incident report"]];
const EVENTS = ["opened", "escalated", "resolved"];

/** A sent mail as the recipient sees it. The sandbox keeps its links and styles inside the frame. */
export function MailFrame({ mail, title }: { mail: Mail; title: string }) {
  return (
    <div className="cell-stack" style={{ gap: 8 }}>
      <div className="small"><span className="faint">Subject</span> <strong>{mail.subject}</strong></div>
      {mail.html ? (
        <iframe title={title} sandbox="" srcDoc={mail.html} style={{ width: "100%", height: 560, border: "1px solid var(--border)", borderRadius: 8, background: "#f9fafb" }} />
      ) : (
        <pre className="mono small" style={{ whiteSpace: "pre-wrap", margin: 0 }}>{mail.text}</pre>
      )}
    </div>
  );
}

/** How the chat card looks in Slack, Teams or Discord: colour bar, headline, the facts, a link. */
function ChatCard({ text, payload, channel }: { text: string; payload: unknown; channel: string }) {
  const [json, setJson] = useState(false);
  const p = payload as Record<string, any>;
  const slack = p?.attachments?.[0]?.blocks ? p.attachments[0] : null;
  const embed = p?.embeds?.[0];
  const teams = p?.attachments?.[0]?.content?.body ? p.attachments[0].content : null;
  const color = slack?.color ?? (embed ? `#${Number(embed.color).toString(16).padStart(6, "0")}` : teams?.body?.[0]?.color === "good" ? "#16a34a" : teams?.body?.[0]?.color === "attention" ? "#dc2626" : "#ea580c");
  const head = embed?.title ?? teams?.body?.[0]?.text ?? String(slack?.blocks?.[0]?.text?.text ?? text).replace(/^\*|\*$/g, "");
  const facts: [string, string][] = embed
    ? embed.fields.map((f: any) => [f.name, f.value])
    : teams
      ? (teams.body[1]?.facts ?? []).map((f: any) => [f.title, f.value])
      : (slack?.blocks?.[1]?.fields ?? []).map((f: any) => String(f.text).replace(/\*/g, "").split("\n") as [string, string]);
  const link = embed?.url ?? teams?.actions?.[0]?.url ?? slack?.blocks?.[2]?.elements?.[0]?.url ?? "";
  return (
    <div className="cell-stack" style={{ gap: 8 }}>
      <div style={{ borderLeft: `4px solid ${color}`, background: "var(--s2)", borderRadius: 6, padding: "10px 14px", maxWidth: 520 }} aria-label={`${channel} message`}>
        <div style={{ fontWeight: 700 }}>{head}</div>
        <div style={{ display: "grid", gridTemplateColumns: "repeat(2, minmax(0, 1fr))", gap: "6px 16px", marginTop: 8 }}>
          {facts.map(([k, v]) => (
            <div key={k} className="cell-stack" style={{ gap: 0 }}><span className="small faint">{k}</span><span className="small">{v}</span></div>
          ))}
        </div>
        {link ? <span className="btn" style={{ marginTop: 10, display: "inline-block" }}>Open incident</span> : <div className="small faint" style={{ marginTop: 8 }}>No “Open incident” button: set General › Public address.</div>}
      </div>
      <button className="btn" type="button" style={{ alignSelf: "flex-start" }} onClick={() => setJson(!json)}>{json ? "Hide" : "Show"} what is sent (JSON)</button>
      {json ? <pre className="mono small" style={{ whiteSpace: "pre-wrap", margin: 0, maxHeight: 280, overflow: "auto" }}>{JSON.stringify(payload, null, 2)}</pre> : null}
    </div>
  );
}

/** Settings › Messages: what ForgeSRE sends when an alarm fires, rendered from the real builders. */
export function MessagePreviews() {
  const [data, setData] = useState<Previews | null>(null);
  const [number, setNumber] = useState("");
  const [tab, setTab] = useState<Tab>("alert");
  const [event, setEvent] = useState("opened");
  const [channel, setChannel] = useState("slack");
  const [error, setError] = useState("");
  const load = (n = "") => getJson<Previews>(`/api/v1/message-previews${n ? `?incident=${encodeURIComponent(n)}` : ""}`).then((d) => (setData(d), setError(""))).catch((e: Error) => setError(e.message));
  useEffect(() => void load(), []);
  const pick = (e: FormEvent) => (e.preventDefault(), load(number.trim()));
  return (
    <>
      <Card title="What gets sent when an alarm fires">
        <ol className="small" style={{ margin: 0, paddingLeft: 18, lineHeight: 1.6 }}>
          <li>The alarm opens an incident. At once, <strong>Tier 1</strong> of its escalation policy gets the <strong>alert mail</strong> below, with a one-click <em>Acknowledge</em> button that needs no sign-in.</li>
          <li>Chat channels that are on (Settings › Notifications) get a short card when the incident opens, at each escalation step and when it resolves. Only the incident line goes out: no logs, no evidence, no ForgeAI text.</li>
          <li>Nobody acknowledged in time? The next tier gets the same mail after the minutes set in its escalation policy (for example Tier 2 after 15 min, Tier 3 after 30). Acknowledging stops it.</li>
          <li>The <strong>incident report</strong> is the longer mail an operator sends from the incident page, for example to a customer or a manager.</li>
        </ol>
        <form className="row" onSubmit={pick}>
          <input className="search" aria-label="Incident number" placeholder="INC-… (empty = newest)" value={number} onChange={(e) => setNumber(e.target.value)} />
          <button className="btn" type="submit">Show for this incident</button>
          {data?.incident ? <Chip tone="off">{data.incident.split("_")[0]}</Chip> : null}
        </form>
        {data?.note ? <span className="small faint">{data.note}</span> : null}
        {data && !data.public_url ? (
          <div className="banner" role="note">General › Public address is empty, so mails have no Acknowledge button and chat cards have no link. Set it to the address people use to open ForgeSRE.</div>
        ) : null}
      </Card>
      {error ? <div className="banner error" role="alert">{error}</div> : null}
      {data ? (
        <Card title="Preview" actions={TABS.map(([k, l]) => <button key={k} className="btn" aria-pressed={tab === k} onClick={() => setTab(k)}>{l}</button>)}>
          {tab === "alert" ? <MailFrame mail={data.alert_email} title="Alert mail preview" /> : null}
          {tab === "report" ? <MailFrame mail={data.incident_report} title="Incident report preview" /> : null}
          {tab === "chat" ? (
            <div className="cell-stack" style={{ gap: 12 }}>
              <div className="row">
                {EVENTS.map((ev) => <button key={ev} className="btn" aria-pressed={event === ev} onClick={() => setEvent(ev)}>{ev[0].toUpperCase() + ev.slice(1)}</button>)}
                <span className="faint">·</span>
                {Object.entries(data.chat.channels).map(([c, label]) => (
                  <button key={c} className="btn" aria-pressed={channel === c} onClick={() => setChannel(c)}>
                    {label}{data.chat.active.includes(c) ? "" : " (off)"}
                  </button>
                ))}
              </div>
              <ChatCard key={`${event}-${channel}`} text={data.chat.events[event].text} payload={data.chat.events[event].payloads[channel]} channel={data.chat.channels[channel]} />
            </div>
          ) : null}
        </Card>
      ) : null}
    </>
  );
}
