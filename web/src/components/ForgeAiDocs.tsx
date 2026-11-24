import { useEffect, useMemo, useState, type DragEvent, type FormEvent } from "react";
import { getJson, postJson } from "../lib/api";
import { clock } from "../lib/format";
import { usePaged } from "./Pager";
import { Card, Chip } from "./ui";

type Doc = { id: number; name: string; title: string; size: number; pages: number; chunks: number; status: string; error: string; model: string; uploaded_by: string; at: string | null };
type Docs = { docs: Doc[]; embedder: boolean; embed_url: string; model: string; pgvector: boolean; can_edit: boolean; usage?: { days: number; given: number; cited: number } };
type Hit = { chunk: number; doc: string; page: number; text: string; score: number };

const TONE: Record<string, "ok" | "warn" | "crit" | "off" | "info"> = { ready: "ok", pending: "info", indexing: "info", error: "crit" };
const ACCEPT = ".pdf,.txt,.md,.html,.htm";

/** Wrap the words of ``query`` found in ``text`` in <mark>, so a test search shows why a piece matched. */
export function highlight(text: string, query: string) {
  const words = query.toLowerCase().split(/\W+/).filter((w) => w.length > 2);
  if (!words.length) return text;
  const re = new RegExp(`(${words.map((w) => w.replace(/[.*+?^${}()|[\]\\]/g, "\\$&")).join("|")})`, "gi");
  return text.split(re).map((part, i) => (i % 2 ? <mark key={i}>{part}</mark> : part));
}

/**
 * Settings › ForgeAI RAG: the manuals and runbooks ForgeAI may quote. Numbers at the top (documents,
 * pieces, how often answers used them), drop files to upload several at once, filter the list,
 * re-index a failed one, and a test search that shows the score and the matching words.
 */
export function ForgeAiDocs() {
  const [data, setData] = useState<Docs | null>(null);
  const [error, setError] = useState("");
  const [busy, setBusy] = useState("");
  const [title, setTitle] = useState("");
  const [filter, setFilter] = useState("");
  const [query, setQuery] = useState("");
  const [hits, setHits] = useState<Hit[] | null>(null);
  const [over, setOver] = useState(false);
  const load = () => getJson<Docs>("/api/v1/forgeai/docs").then((d) => (setData({ ...d, docs: d.docs ?? [] }), setError(""))).catch((e: Error) => setError(e.message));
  useEffect(() => void load(), []);
  useEffect(() => {
    if (!data?.docs.some((d) => d.status === "pending" || d.status === "indexing")) return;
    const t = window.setTimeout(load, 4000);
    return () => window.clearTimeout(t);
  }, [data]);

  const uploadOne = async (file: File, name: string) => {
    const form = new FormData();
    form.append("file", file);
    const r = await fetch(`/api/v1/forgeai/docs?title=${encodeURIComponent(name)}`, { method: "POST", body: form, credentials: "same-origin" });
    if (!r.ok) throw new Error(`${file.name}: ${(await r.json().catch(() => ({}))).detail ?? r.statusText}`);
  };
  const upload = async (files: File[]) => {
    if (!files.length) return;
    setError("");
    const failed: string[] = [];
    for (const [i, f] of files.entries()) {
      setBusy(`Uploading ${i + 1} of ${files.length}: ${f.name}`);
      // the title field names a single file; several files keep their own names
      await uploadOne(f, files.length === 1 ? title : "").catch((e: Error) => failed.push(e.message));
    }
    setBusy("");
    setTitle("");
    if (failed.length) setError(failed.join(" · "));
    await load();
  };
  const drop = (e: DragEvent) => (e.preventDefault(), setOver(false), void upload([...e.dataTransfer.files]));
  const remove = (id: number) => getJson(`/api/v1/forgeai/docs/${id}`, { method: "DELETE" }).then(load).catch((e: Error) => setError(e.message));
  const reindex = (id: number) => postJson(`/api/v1/forgeai/docs/${id}/reindex`, {}).then(load).catch((e: Error) => setError(e.message));
  const search = (e: FormEvent) => {
    e.preventDefault();
    setHits(null);
    getJson<{ hits: Hit[] }>(`/api/v1/forgeai/docs/search?q=${encodeURIComponent(query)}`).then((r) => setHits(r.hits)).catch((err: Error) => setError(err.message));
  };

  const docs = data?.docs ?? [];
  const shown = useMemo(() => docs.filter((d) => !filter || `${d.title} ${d.name}`.toLowerCase().includes(filter.toLowerCase())), [docs, filter]);
  const pgDocs = usePaged(shown, "docs", "documents");
  const ready = docs.filter((d) => d.status === "ready");
  const failed = docs.filter((d) => d.status === "error").length;
  const pieces = ready.reduce((s, d) => s + d.chunks, 0);
  const pages = ready.reduce((s, d) => s + d.pages, 0);
  return (
    <Card title="ForgeAI RAG · documents" actions={data ? <Chip tone={data.embedder ? "ok" : "off"}>{data.embedder ? "Search on" : "Search off"}</Chip> : null}>
      <span className="small muted">
        Vendor manuals and runbooks ForgeAI may quote. Each file is cut into pieces of about 1 200 characters and turned into vectors by
        the local embedding model ({data?.model ?? "…"}{data?.pgvector ? ", pgvector" : ""}). For every incident and question the three closest
        pieces go to ForgeAI, which cites them as [D1]–[D3]. Nothing leaves this server.
      </span>
      {data ? (
        <div className="strip" role="group" aria-label="Document numbers">
          <div className="stat"><span className="stat-label">Documents</span><span className="stat-value">{docs.length}</span><span className="small faint">{ready.length} ready{failed ? ` · ${failed} failed` : ""}</span></div>
          <div className="stat"><span className="stat-label">Pieces</span><span className="stat-value">{pieces}</span><span className="small faint">{pages} pages</span></div>
          <div className="stat"><span className="stat-label">Given to ForgeAI · {data.usage?.days ?? 7} days</span><span className="stat-value">{data.usage?.given ?? 0}</span><span className="small faint">questions with excerpts</span></div>
          <div className="stat"><span className="stat-label">Cited in answers</span><span className="stat-value">{data.usage?.cited ?? 0}</span><span className="small faint">answers with [D1]…</span></div>
        </div>
      ) : null}
      {data && !data.embedder ? (
        <div className="banner warn" role="status">
          The embedding model does not answer, so documents are not searched and ForgeAI gets none. It runs as the <code className="mono">embed</code> container
          with the local model: <code className="mono">./forgesre fetch-llm</code> downloads and starts it; <code className="mono">./forgesre doctor</code> shows the row <em>Document search</em>.
        </div>
      ) : null}
      {data?.can_edit ? (
        <div className={`rag-drop${over ? " over" : ""}`} onDragOver={(e) => (e.preventDefault(), setOver(true))} onDragLeave={() => setOver(false)} onDrop={drop}>
          <strong>Drop files here</strong>
          <span className="small faint">PDF with a text layer, text, Markdown or HTML, up to 30 MB each; several at once is fine.</span>
          <div className="row" style={{ gap: 8, flexWrap: "wrap", justifyContent: "center" }}>
            <input className="search" aria-label="Document title" placeholder="Title for a single file (optional)" value={title}
              onChange={(e) => setTitle(e.target.value)} style={{ flex: "1 1 240px", minWidth: 0, maxWidth: 360 }} />
            <input type="file" multiple aria-label="Document files" accept={ACCEPT} disabled={Boolean(busy)}
              onChange={(e) => { const files = [...(e.target.files ?? [])]; e.target.value = ""; void upload(files); }} />
          </div>
          {busy ? <span className="small" role="status">{busy}</span> : null}
        </div>
      ) : <span className="small faint">Engineers and Admins upload documents.</span>}
      {error ? <span className="small" role="alert" style={{ color: "var(--crit)" }}>{error}</span> : null}
      {docs.length ? (<>
        <input className="search" type="search" aria-label="Filter documents" placeholder="Filter by title or file name" value={filter} onChange={(e) => setFilter(e.target.value)} style={{ maxWidth: 360 }} />
        <div className="table-wrap"><table className="data" style={{ minWidth: 700 }}>
          <thead><tr><th>Document</th><th style={{ width: 100 }}>State</th><th style={{ width: 150 }}>Size</th><th style={{ width: 150 }}>Added</th><th style={{ width: 170 }}></th></tr></thead>
          <tbody>
            {pgDocs.page.map((d) => (
              <tr key={d.id}>
                <td><div className="cell-stack"><span>{d.title}</span><span className="small faint">{d.name}{d.error ? ` · ${d.error}` : ""}</span></div></td>
                <td><Chip tone={TONE[d.status] ?? "off"}>{d.status}</Chip></td>
                <td className="small muted">{d.status === "ready" ? `${d.pages} pages · ${d.chunks} pieces` : `${Math.ceil(d.size / 1024)} KB`}</td>
                <td className="small muted">{clock(d.at)}<br />{d.uploaded_by}</td>
                <td>{data?.can_edit ? (
                  <div className="row" style={{ gap: 6 }}>
                    {d.status === "error" || d.status === "pending" ? <button className="btn" type="button" onClick={() => void reindex(d.id)}>Index again</button> : null}
                    <button className="btn" type="button" onClick={() => void remove(d.id)}>Remove</button>
                  </div>
                ) : null}</td>
              </tr>
            ))}
          </tbody>
        </table></div>
        {pgDocs.pager}</>
      ) : data ? <span className="muted">No documents yet. Start with the manuals of the hardware that alarms most (Reports › Noisiest assets).</span> : null}
      {ready.length ? (
        <form className="cell-stack" style={{ gap: 8, borderTop: "1px solid var(--border)", paddingTop: 10 }} onSubmit={search}>
          <strong className="small">Test the search</strong>
          <span className="small faint">Type what an alarm or an operator would say; you see the pieces ForgeAI would get, best first.</span>
          <div className="row" style={{ gap: 8 }}>
            <input className="search" aria-label="Try the document search" placeholder="e.g. degraded RAID virtual disk" value={query}
              onChange={(e) => setQuery(e.target.value)} style={{ flex: "1 1 260px", minWidth: 0 }} />
            <button className="btn" type="submit" disabled={!query.trim()}>Search</button>
          </div>
          {hits ? hits.length ? hits.map((h, i) => (
            <div key={h.chunk} className="rag-hit">
              <div className="row small" style={{ gap: 8, justifyContent: "space-between" }}>
                <span><strong>{i < 3 ? `D${i + 1}` : `#${i + 1}`}</strong> · {h.doc} · page {h.page}{i >= 3 ? " · not sent (only the best three are)" : ""}</span>
                <span className="faint">match {Math.round(h.score * 100)}%</span>
              </div>
              <div className="rag-score" aria-hidden="true"><i style={{ width: `${Math.max(2, Math.round(h.score * 100))}%` }} /></div>
              <span className="small">{highlight(h.text.slice(0, 500), query)}{h.text.length > 500 ? "…" : ""}</span>
            </div>
          )) : <span className="small faint">Nothing close.</span> : null}
        </form>
      ) : null}
    </Card>
  );
}
