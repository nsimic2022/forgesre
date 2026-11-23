import { useCallback, useEffect, useState } from "react";
import { useSearchParams } from "react-router-dom";
import { getJson, postJson } from "../lib/api";
import { clock } from "../lib/format";
import { useRoleRank } from "../lib/me";
import { ForgeAiFound } from "./AskForgeAi";
import { deleteChat, ForgeAiChat, type ChatDetail } from "./ForgeAiChat";
import { usePaged } from "./Pager";

type ChatRow = { id: number; title: string; incident: string | null; incident_label: string; updated_at: string | null };
type ChatList = { chats: ChatRow[]; pending: number; local_on: boolean; can_cloud: boolean };

/**
 * ForgeAI chat on Investigations › Ask ForgeAI: your chats on the left, the open one on the right
 * (``?chat=`` in the URL). A chat is general or about one incident; an incident chat also shows
 * what ForgeAI found on its own. ``about`` (from the Ask button on a row) opens or reuses the chat
 * about that incident. Chats are private to you.
 */
export function ForgeAiWorkspace({ about = "", aboutKey = 0 }: { about?: string; aboutKey?: number }) {
  const rank = useRoleRank();
  const [params, setParams] = useSearchParams();
  const [list, setList] = useState<ChatList | null>(null);
  const [error, setError] = useState("");
  const [incident, setIncident] = useState("");
  const [asking, setAsking] = useState(0);
  const open = Number(params.get("chat") || 0) || null;
  const chats = Array.isArray(list?.chats) ? list!.chats : [];
  const pg = usePaged(chats, "forgeai-chats", "chats");

  const setChat = useCallback((id: number | null, replace = false) => {
    setParams((p) => {
      const next = new URLSearchParams(p);
      if (id) next.set("chat", String(id));
      else next.delete("chat");
      return next;
    }, { replace });
  }, [setParams]);
  const load = useCallback(() => getJson<ChatList>("/api/v1/forgeai/chats").then(setList).catch((e: Error) => setError(e.message)), []);
  useEffect(() => {
    if (rank >= 1) void load();
  }, [rank, load]);
  useEffect(() => {
    if (!open && chats.length && !about) setChat(chats[0].id, true);
  }, [open, chats, about, setChat]);

  const start = useCallback(async (number = "") => {
    setError("");
    try {
      const chat = await postJson<{ id: number }>("/api/v1/forgeai/chats", { incident: number.trim(), reuse: Boolean(number.trim()) });
      setIncident("");
      await load();
      setChat(chat.id);
    } catch (e) {
      setError((e as Error).message);
    }
  }, [load, setChat]);
  useEffect(() => {
    if (rank >= 1 && about.trim()) void start(about);
  }, [rank, about, aboutKey, start]);

  const remove = async (id: number) => {
    await deleteChat(id).catch((e: Error) => setError(e.message));
    setAsking(0);
    if (open === id) setChat(null);
    await load();
  };
  // keep the title in the list in step after the first message names the chat
  const changed = useCallback((c: ChatDetail) => setList((l) => l && { ...l, chats: l.chats.map((r) => (r.id === c.id ? { ...r, title: c.title } : r)) }), []);

  if (rank < 0) return <span className="muted">Loading…</span>;
  if (rank < 1) return <span className="small faint">Analysts and up can chat with ForgeAI.</span>;
  const current = chats.find((c) => c.id === open) ?? null;
  return (
    <>
      {error ? <div className="banner error" role="alert">{error}</div> : null}
      {list && !list.local_on ? <div className="banner">The local model is off: turn it on in Settings › RCA / AI.</div> : null}
      <div className="chat-page">
        <aside className="chat-threads" aria-label="Your chats">
          <button className="btn primary" type="button" onClick={() => void start()}>New chat</button>
          <form className="row" style={{ gap: 6 }} onSubmit={(e) => (e.preventDefault(), void start(incident))}>
            <input className="search" aria-label="Chat about incident" placeholder="INC-… chat about an incident" value={incident}
              onChange={(e) => setIncident(e.target.value)} style={{ minWidth: 0, flex: 1 }} />
            <button className="btn" type="submit" disabled={!incident.trim()}>Open</button>
          </form>
          <div className="chat-thread-list">
            {list === null ? <span className="muted">Loading…</span> : chats.length ? (<>{pg.page.map((c) => (
              <div key={c.id} className={`chat-thread${open === c.id ? " on" : ""}`}>
                <button type="button" className="chat-thread-open" onClick={() => setChat(c.id)} aria-current={open === c.id}>
                  <span className="chat-thread-title">{c.title}</span>
                  <span className="small faint">{c.incident ? `${c.incident.split("_")[0]} · ` : ""}{c.updated_at ? clock(c.updated_at) : ""}</span>
                </button>
                {asking === c.id ? (
                  <span className="row" style={{ gap: 4 }}>
                    <button className="btn" type="button" onClick={() => void remove(c.id)}>Delete</button>
                    <button className="btn" type="button" onClick={() => setAsking(0)}>Keep</button>
                  </span>
                ) : <button className="btn chat-thread-x" type="button" aria-label={`Delete chat ${c.title}`} onClick={() => setAsking(c.id)}>×</button>}
              </div>
            ))}{pg.pager}</>) : <span className="small faint">No chats yet.</span>}
          </div>
        </aside>
        <section className="chat-main">
          {open ? <ForgeAiChat chatId={open} onChanged={changed} /> : (
            <div className="chat-empty"><strong>Start a chat</strong><span className="small muted">Press New chat, Ask on a row above, or open a chat about an incident.</span></div>
          )}
        </section>
      </div>
      {current?.incident ? <ForgeAiFound number={current.incident} /> : null}
    </>
  );
}
