import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { getJson, postJson } from "../lib/api";
import { clock } from "../lib/format";
import { useRoleRank } from "../lib/me";
import { Rate } from "./ForgeAiCard";
import { ForgeAiPulse } from "./ForgeAiPulse";
import { Chip } from "./ui";

export type ChatSkill = { slug: string; name: string; task: string; task_label: string };
export type ChatMessage = {
  id: number; role: "user" | "assistant"; text: string; skill: string; cloud: boolean; status: string; call_id: number | null; at: string | null;
  phase?: string; thinking?: string; started_at?: string | null;
  queue?: { ahead: number; running: { kind: string; seconds: number } | null; timeout_s: number };
};

const PHASE: Record<string, string> = {
  queued: "Waiting in the ForgeAI queue",
  waiting: "The model is reading the question",
  thinking: "Thinking",
  writing: "Writing the answer",
  checking: "Checking the answer",
};
const mmss = (s: number) => `${Math.floor(s / 60)}:${String(Math.floor(s % 60)).padStart(2, "0")}`;

/** Ticks once a second while ``on``, so elapsed times move between polls. */
function useNow(on: boolean): number {
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    if (!on) return;
    const t = setInterval(() => setNow(Date.now()), 1000);
    return () => clearInterval(t);
  }, [on]);
  return now;
}

/**
 * A pending answer: where it is (queue, reading, thinking, writing), how long it has run against the
 * timeout, and the tail of the model's reasoning, folded, while it thinks.
 */
function Progress({ m, now }: { m: ChatMessage; now: number }) {
  const phase = m.phase || "queued";
  const since = m.started_at ? Math.max(0, (now - Date.parse(m.started_at)) / 1000) : 0;
  const limit = m.queue?.timeout_s || 0;
  const q = m.queue;
  const where =
    phase === "queued" && q
      ? q.ahead ? `${q.ahead} ahead${q.running ? ` (the model is busy with ${q.running.kind === "forgeai_chat" ? "another chat" : q.running.kind} for ${q.running.seconds} s)` : ""}` : "next"
      : phase !== "queued" ? `${mmss(since)}${limit ? ` of max ${mmss(limit)}` : ""}` : "";
  return (
    <div className="chat-progress" aria-live="polite">
      <span className="row" style={{ gap: 8 }}>
        <span className="chat-typing" aria-hidden="true"><i /><i /><i /></span>
        <span>{PHASE[phase] ?? phase}</span>
        {where ? <span className="small faint">{where}</span> : null}
      </span>
      {limit && phase !== "queued" ? (
        <span className="chat-bar" aria-hidden="true"><i style={{ width: `${Math.min(100, (since / limit) * 100)}%` }} className={since > limit * 0.8 ? "late" : ""} /></span>
      ) : null}
      {m.thinking ? (
        <details className="chat-think">
          <summary className="small faint">Show what it is thinking</summary>
          <pre>…{m.thinking.slice(-600)}</pre>
        </details>
      ) : null}
    </div>
  );
}
export type ChatDetail = { id: number; title: string; incident: string | null; incident_label: string; updated_at: string | null; messages: ChatMessage[]; skills: ChatSkill[] };
type Cloud = { enabled: boolean; ready: boolean; provider: string; model: string; host: string; can_use: boolean };

const POLL_MS = 2500;
const TEXT_MAX = 2000;

/** Groups skills for the picker: chat first, then by task label. */
function grouped(skills: ChatSkill[]): [string, ChatSkill[]][] {
  const out = new Map<string, ChatSkill[]>();
  for (const s of [...skills].sort((a, b) => (a.task === "chat" ? -1 : b.task === "chat" ? 1 : a.task_label.localeCompare(b.task_label)))) {
    const key = s.task === "chat" ? "Chat" : s.task_label;
    out.set(key, [...(out.get(key) ?? []), s]);
  }
  return [...out.entries()];
}

/**
 * One ForgeAI conversation. Type a message, or start with "/" to pick a skill (on an incident chat
 * a skill re-runs that kind of answer, e.g. "cause and first checks", as a trial). Answers come from a
 * background job; the thread polls while one is pending. Engineers can switch one message to the
 * paid cloud model, and use an incident answer as the incident's answer.
 */
export function ForgeAiChat({ chatId, onChanged }: { chatId: number; onChanged?: (chat: ChatDetail) => void }) {
  const rank = useRoleRank();
  const [chat, setChat] = useState<ChatDetail | null>(null);
  const [text, setText] = useState("");
  const [skill, setSkill] = useState<ChatSkill | null>(null);
  const [menu, setMenu] = useState(false);
  const [cursor, setCursor] = useState(0);
  const [cloud, setCloud] = useState<Cloud | null>(null);
  const [useCloud, setUseCloud] = useState(false);
  const [error, setError] = useState("");
  const [sending, setSending] = useState(false);
  const [used, setUsed] = useState<number[]>([]);
  const end = useRef<HTMLDivElement>(null);
  const box = useRef<HTMLTextAreaElement>(null);

  const load = useCallback(
    () => getJson<ChatDetail>(`/api/v1/forgeai/chats/${chatId}`).then((c) => (setChat(c), onChanged?.(c))).catch((e: Error) => setError(e.message)),
    [chatId, onChanged],
  );
  useEffect(() => {
    setChat(null);
    setError("");
    setSkill(null);
    void load();
  }, [chatId]); // eslint-disable-line react-hooks/exhaustive-deps
  useEffect(() => void getJson<{ cloud?: Cloud }>("/api/v1/forgeai/status").then((s) => setCloud(s.cloud ?? null)).catch(() => setCloud(null)), []);
  const messages = chat?.messages ?? [];
  const waiting = Boolean(messages.some((m) => m.status === "pending"));
  const now = useNow(waiting);
  useEffect(() => {
    if (!waiting) return;
    const t = setInterval(() => void load(), POLL_MS);
    return () => clearInterval(t);
  }, [waiting, load]);
  useEffect(() => end.current?.scrollIntoView?.({ block: "nearest" }), [messages.length, waiting]);

  const skills = chat?.skills ?? [];
  const bySlug = useMemo(() => new Map(skills.map((s) => [s.slug, s])), [skills]);
  // "/disk" filters the menu; the slash only opens it at the start of the message
  const slash = text.startsWith("/") ? text.slice(1).toLowerCase() : null;
  const matches = slash === null ? skills : skills.filter((s) => s.slug.includes(slash) || s.name.toLowerCase().includes(slash));
  const showMenu = menu || (slash !== null && !skill);
  const cloudOk = Boolean(cloud?.enabled && cloud.ready && cloud.can_use);

  const pick = (s: ChatSkill) => {
    setSkill(s.task === "chat" && s.slug === "chat-generic" ? null : s);
    setText((t) => (t.startsWith("/") ? "" : t));
    setMenu(false);
    setCursor(0);
    box.current?.focus();
  };
  const send = async () => {
    if (sending || (!text.trim() && !(skill && skill.task !== "chat"))) return;
    setSending(true);
    setError("");
    try {
      await postJson(`/api/v1/forgeai/chats/${chatId}/messages`, { text: text.trim(), skill: skill?.slug ?? "", cloud: useCloud });
      setText("");
      setSkill(null);
      setUseCloud(false);
      await load();
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setSending(false);
    }
  };
  const onKey = (e: React.KeyboardEvent<HTMLTextAreaElement>) => {
    if (showMenu && matches.length) {
      if (e.key === "ArrowDown" || e.key === "ArrowUp") {
        e.preventDefault();
        setCursor((c) => (c + (e.key === "ArrowDown" ? 1 : matches.length - 1)) % matches.length);
        return;
      }
      if (e.key === "Enter" || e.key === "Tab") {
        e.preventDefault();
        pick(matches[Math.min(cursor, matches.length - 1)]);
        return;
      }
    }
    if (e.key === "Escape") setMenu(false);
    if (e.key === "Enter" && !e.shiftKey) {
      e.preventDefault();
      void send();
    }
  };
  const use = (callId: number) =>
    postJson(`/api/v1/forgeai/calls/${callId}/use`, {}).then(() => setUsed((u) => [...u, callId])).catch((e: Error) => setError(e.message));

  if (!chat) return <div className="chat"><span className="muted" style={{ padding: 16 }}>{error || "Loading…"}</span></div>;
  return (
    <div className="chat">
      <ForgeAiPulse />
      <div className="chat-log" aria-live="polite">
        {messages.length ? null : (
          <div className="chat-empty">
            <strong>{chat.incident ? `Ask about ${chat.incident_label}` : "Ask ForgeAI anything"}</strong>
            <span className="small muted">
              {chat.incident
                ? "ForgeAI reads this incident's facts, playbook and similar incidents with every message. Type / for skills."
                : "General questions about networks, servers, databases and monitoring. Type / to pick a skill, or start a chat from an incident to talk about its facts."}
            </span>
          </div>
        )}
        {messages.map((m) => {
          const s = bySlug.get(m.skill);
          return (
            <div key={m.id} className={`chat-msg ${m.role}${m.status === "error" ? " failed" : ""}`}>
              <div className="chat-bubble">
                {m.status === "pending" ? <Progress m={m} now={now} /> : m.text}
                {m.status !== "pending" && m.thinking ? (
                  <details className="chat-think">
                    <summary className="small faint">How ForgeAI got there</summary>
                    <pre>{m.thinking}</pre>
                  </details>
                ) : null}
                {m.status === "error" && /within \d+ s|timed out|timeout/i.test(m.text) ? (
                  <span className="small faint" style={{ display: "block", marginTop: 6 }}>
                    The model did not finish in time. A small CPU model may need a longer Model timeout (Settings › RCA / AI), or thinking turned off.
                  </span>
                ) : null}
              </div>
              <div className="chat-meta small faint">
                {m.role === "assistant" ? "ForgeAI" : "You"}
                {m.skill && m.skill !== "chat-generic" ? <> · {s?.name ?? m.skill}</> : null}
                {m.cloud ? <> · <Chip tone="warn">cloud</Chip></> : null}
                {m.at ? <> · {clock(m.at)}</> : null}
                {m.role === "assistant" && m.status === "done" && m.call_id ? (
                  <>
                    <Rate callId={m.call_id} />
                    {rank >= 2 && s?.task === "incident" ? (
                      used.includes(m.call_id) ? <span>Now the incident's answer</span>
                        : <button className="btn" type="button" onClick={() => void use(m.call_id!)}>Use as the incident's answer</button>
                    ) : null}
                  </>
                ) : null}
              </div>
            </div>
          );
        })}
        {waiting ? <span className="small faint" style={{ alignSelf: "center" }}>You can leave; the answer stays in this chat.</span> : null}
        <div ref={end} />
      </div>
      <div className="chat-compose">
        {showMenu && matches.length ? (
          <div className="chat-menu" role="listbox" aria-label="Skills">
            {grouped(matches).map(([group, rows]) => (
              <div key={group}>
                <div className="chat-menu-group small faint">{group}</div>
                {rows.map((s) => (
                  <button key={s.slug} type="button" role="option" aria-selected={matches[cursor]?.slug === s.slug}
                    className={`chat-menu-item${matches[cursor]?.slug === s.slug ? " on" : ""}`} onMouseDown={(e) => (e.preventDefault(), pick(s))}>
                    <span>{s.name}</span><span className="small faint">/{s.slug}</span>
                  </button>
                ))}
              </div>
            ))}
          </div>
        ) : null}
        {skill ? (
          <div className="row" style={{ gap: 6 }}>
            <span className="chat-skill">
              {skill.task_label}: {skill.name}
              <button type="button" aria-label="Remove skill" onClick={() => setSkill(null)}>×</button>
            </span>
            <span className="small faint">{skill.task === "chat" ? "" : "Send as is, or add a note."}</span>
          </div>
        ) : null}
        <textarea ref={box} className="chat-input" aria-label="Message to ForgeAI" rows={2} maxLength={TEXT_MAX} value={text}
          placeholder={chat.incident ? "Ask about this incident, or type / for skills" : "Ask ForgeAI…  (type / for skills, Shift+Enter for a new line)"}
          onChange={(e) => (setText(e.target.value), setCursor(0))} onKeyDown={onKey} disabled={rank >= 0 && rank < 1} />
        <div className="row" style={{ justifyContent: "space-between", gap: 8, flexWrap: "wrap" }}>
          <div className="row" style={{ gap: 6 }}>
            <button className="btn" type="button" aria-expanded={showMenu} onClick={() => setMenu((v) => !v)}>Skills</button>
            {cloudOk ? (
              <div className="chat-seg" role="group" aria-label="Model">
                <button type="button" aria-pressed={!useCloud} className={useCloud ? "" : "on"} onClick={() => setUseCloud(false)}>Local</button>
                <button type="button" aria-pressed={useCloud} className={useCloud ? "on" : ""} onClick={() => setUseCloud(true)}
                  title={`Sends this message${chat.incident ? " and the incident's facts" : ""} to ${cloud!.host}`}>Cloud</button>
              </div>
            ) : null}
            <span className="small faint">
              {useCloud ? `This message goes to ${cloud!.provider} · ${cloud!.model}; it costs money and is audited.` : "Local model: nothing leaves this server."}
            </span>
          </div>
          <button className="btn primary" type="button" onClick={() => void send()} disabled={sending || (!text.trim() && !(skill && skill.task !== "chat"))}>
            {sending ? "Sending…" : "Send"}
          </button>
        </div>
        {rank >= 0 && rank < 1 ? <span className="small faint">Analysts and up can chat with ForgeAI.</span> : null}
        {error ? <span className="small" role="alert" style={{ color: "var(--crit)" }}>{error}</span> : null}
      </div>
    </div>
  );
}

/** Opens (or reuses) the signed-in person's chat about one incident and shows it. */
export function IncidentChat({ number }: { number: string }) {
  const [id, setId] = useState<number | null>(null);
  const [error, setError] = useState("");
  useEffect(() => {
    setId(null);
    postJson<{ id: number }>("/api/v1/forgeai/chats", { incident: number, reuse: true }).then((c) => setId(c.id)).catch((e: Error) => setError(e.message));
  }, [number]);
  if (error) return <span className="small muted">{error}</span>;
  return id === null ? <span className="muted">Loading…</span> : (
    <div className="cell-stack" style={{ gap: 6 }}>
      <ForgeAiChat chatId={id} />
      <a className="small" href={`/app/investigations?chat=${id}#ask-forgeai`}>Open in Investigations › Ask ForgeAI</a>
    </div>
  );
}

export async function deleteChat(id: number): Promise<void> {
  await getJson(`/api/v1/forgeai/chats/${id}`, { method: "DELETE" });
}
