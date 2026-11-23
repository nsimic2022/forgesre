import { useCallback, useEffect, useState } from "react";
import { useSearchParams } from "react-router-dom";
import { deleteChat, ForgeAiChat, type ChatDetail } from "../components/ForgeAiChat";
import { usePaged } from "../components/Pager";
import { getJson, postJson } from "../lib/api";
import { clock } from "../lib/format";
import { useRoleRank } from "../lib/me";

type ChatRow = { id: number; title: string; incident: string | null; incident_label: string; updated_at: string | null };
type ChatList = { chats: ChatRow[]; pending: number; local_on: boolean; can_cloud: boolean };

/**
 * ForgeAI chat: your conversations with the model on the left, the open one on the right. A chat is
 * general, or about one incident (then ForgeAI reads its facts). Chats are private to you.
 */
export function ForgeAiPage() {
  const rank = useRoleRank();
  const [params, setParams] = useSearchParams();
  const [list, setList] = useState<ChatList | null>(null);
  const [error, setError] = useState("");
  const [incident, setIncident] = useState("");
  const [asking, setAsking] = useState(0);
  const open = Number(params.get("chat") || 0) || null;
  const pg = usePaged(list?.chats ?? [], "forgeai-chats", "chats");

  const load = useCallback(() => getJson<ChatList>("/api/v1/forgeai/chats").then(setList).catch((e: Error) => setError(e.message)), []);
  useEffect(() => {
    document.title = "ForgeAI · ForgeSRE";
    if (rank >= 1) void load();
  }, [rank, load]);
  useEffect(() => {
    if (!open && list?.chats.length) setParams({ chat: String(list.chats[0].id) }, { replace: true });
  }, [open, list, setParams]);

  const start = async (about = "") => {
    setError("");
    try {
      const chat = await postJson<{ id: number }>("/api/v1/forgeai/chats", { incident: about.trim(), reuse: Boolean(about.trim()) });
      setIncident("");
      await load();
      setParams({ chat: String(chat.id) });
    } catch (e) {
      setError((e as Error).message);
    }
  };
  const remove = async (id: number) => {
    await deleteChat(id).catch((e: Error) => setError(e.message));
    setAsking(0);
    if (open === id) setParams({});
    await load();
  };
  // keep the title in the list in step after the first message names the chat
  const changed = useCallback((c: ChatDetail) => setList((l) => l && { ...l, chats: l.chats.map((r) => (r.id === c.id ? { ...r, title: c.title } : r)) }), []);

  if (rank >= 0 && rank < 1) {
    return (
      <header className="page-head"><div><h1>ForgeAI</h1><span className="muted">Analysts and up can chat with ForgeAI.</span></div></header>
    );
  }
  return (
    <>
      <header className="page-head">
        <div>
          <h1>ForgeAI</h1>
          <span className="muted">Ask anything, or talk about one incident. Type / for skills. Your chats are private to you.</span>
        </div>
      </header>
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
            {list === null ? <span className="muted">Loading…</span> : list.chats.length ? (<>{pg.page.map((c) => (
              <div key={c.id} className={`chat-thread${open === c.id ? " on" : ""}`}>
                <button type="button" className="chat-thread-open" onClick={() => setParams({ chat: String(c.id) })} aria-current={open === c.id}>
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
            <div className="chat-empty"><strong>Start a chat</strong><span className="small muted">Press New chat, or open a chat about an incident.</span></div>
          )}
        </section>
      </div>
    </>
  );
}
