import { useEffect, useState, type FormEvent } from "react";
import { getJson } from "../lib/api";
import { clock } from "../lib/format";
import { usePaged } from "./Pager";
import { Card, Chip } from "./ui";

type Doc = { id: number; name: string; title: string; size: number; pages: number; chunks: number; status: string; error: string; model: string; uploaded_by: string; at: string | null };
type Docs = { docs: Doc[]; embedder: boolean; embed_url: string; model: string; pgvector: boolean; can_edit: boolean };
type Hit = { chunk: number; doc: string; page: number; text: string; score: number };

const TONE: Record<string, "ok" | "warn" | "crit" | "off" | "info"> = { ready: "ok", pending: "info", indexing: "info", error: "crit" };

/** Settings › ForgeAI: vendor manuals ForgeAI may cite (RAG). Upload, state, remove, and a test search. */
export function ForgeAiDocs() {
  const [data, setData] = useState<Docs | null>(null);
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const [title, setTitle] = useState("");
  const [query, setQuery] = useState("");
  const [hits, setHits] = useState<Hit[] | null>(null);
  const load = () => getJson<Docs>("/api/v1/forgeai/docs").then((d) => (setData({ ...d, docs: d.docs ?? [] }), setError(""))).catch((e: Error) => setError(e.message));
  useEffect(() => void load(), []);
  useEffect(() => {
    if (!data?.docs.some((d) => d.status === "pending" || d.status === "indexing")) return;
    const t = window.setTimeout(load, 4000);
    return () => window.clearTimeout(t);
  }, [data]);

  const upload = (file: File) => {
    const form = new FormData();
    form.append("file", file);
    setBusy(true);
    setError("");
    fetch(`/api/v1/forgeai/docs?title=${encodeURIComponent(title)}`, { method: "POST", body: form, credentials: "same-origin" })
      .then(async (r) => {
        if (!r.ok) throw new Error((await r.json().catch(() => ({}))).detail ?? r.statusText);
        setTitle("");
        return load();
      })
      .catch((e: Error) => setError(e.message))
      .finally(() => setBusy(false));
  };
  const remove = (id: number) => getJson(`/api/v1/forgeai/docs/${id}`, { method: "DELETE" }).then(load).catch((e: Error) => setError(e.message));
  const search = (e: FormEvent) => {
    e.preventDefault();
    setHits(null);
    getJson<{ hits: Hit[] }>(`/api/v1/forgeai/docs/search?q=${encodeURIComponent(query)}`).then((r) => setHits(r.hits)).catch((err: Error) => setError(err.message));
  };

  const pgDocs = usePaged(data?.docs ?? [], "docs", "documents");
  return (
    <Card title="ForgeAI · documents" actions={data ? <Chip tone={data.embedder ? "ok" : "off"}>{data.embedder ? "Search on" : "Search off"}</Chip> : null}>
      <span className="small muted">
        Vendor manuals and runbooks ForgeAI may quote. They are cut into short pieces and searched with a local embedding model
        ({data?.model ?? "…"}{data?.pgvector ? ", pgvector" : ""}); the three closest pieces go to ForgeAI with each incident and question, cited as [D1]–[D3].
        Nothing leaves this server. PDF (with a text layer), text, Markdown or HTML, up to 30 MB.
      </span>
      {data?.can_edit ? (
        <div className="row" style={{ gap: 8, flexWrap: "wrap" }}>
          <input className="search" aria-label="Document title" placeholder="Title (optional), e.g. Dell R750 owner's manual" value={title}
            onChange={(e) => setTitle(e.target.value)} style={{ flex: "1 1 260px", minWidth: 0 }} />
          <input type="file" aria-label="Document file" accept=".pdf,.txt,.md,.html,.htm" disabled={busy}
            onChange={(e) => (e.target.files?.[0] ? upload(e.target.files[0]) : null, (e.target.value = ""))} />
          {busy ? <span className="small faint">Uploading…</span> : null}
        </div>
      ) : <span className="small faint">Engineers and Admins upload documents.</span>}
      {error ? <span className="small" role="alert" style={{ color: "var(--crit)" }}>{error}</span> : null}
      {data && data.docs.length ? (<>
        <div className="table-wrap"><table className="data" style={{ minWidth: 640 }}>
          <thead><tr><th>Document</th><th style={{ width: 100 }}>State</th><th style={{ width: 120 }}>Size</th><th style={{ width: 150 }}>Added</th><th style={{ width: 90 }}></th></tr></thead>
          <tbody>
            {pgDocs.page.map((d) => (
              <tr key={d.id}>
                <td><div className="cell-stack"><span>{d.title}</span><span className="small faint">{d.name}{d.error ? ` · ${d.error}` : ""}</span></div></td>
                <td><Chip tone={TONE[d.status] ?? "off"}>{d.status}</Chip></td>
                <td className="small muted">{d.status === "ready" ? `${d.pages} pages · ${d.chunks} pieces` : `${Math.ceil(d.size / 1024)} KB`}</td>
                <td className="small muted">{clock(d.at)}<br />{d.uploaded_by}</td>
                <td>{data.can_edit ? <button className="btn" type="button" onClick={() => void remove(d.id)}>Remove</button> : null}</td>
              </tr>
            ))}
          </tbody>
        </table></div>
        {pgDocs.pager}</>
      ) : data ? <span className="muted">No documents yet.</span> : null}
      {data && data.docs.some((d) => d.status === "ready") ? (
        <form className="cell-stack" style={{ gap: 6, borderTop: "1px solid var(--border)", paddingTop: 8 }} onSubmit={search}>
          <div className="row" style={{ gap: 8 }}>
            <input className="search" aria-label="Try the document search" placeholder="Try it: e.g. degraded RAID virtual disk" value={query}
              onChange={(e) => setQuery(e.target.value)} style={{ flex: "1 1 260px", minWidth: 0 }} />
            <button className="btn" type="submit" disabled={!query.trim()}>Search</button>
          </div>
          {hits ? hits.length ? hits.map((h) => (
            <div key={h.chunk} className="cell-stack" style={{ gap: 2 }}>
              <span className="small faint">{h.doc} · page {h.page} · match {Math.round(h.score * 100)}%</span>
              <span className="small">{h.text.slice(0, 400)}{h.text.length > 400 ? "…" : ""}</span>
            </div>
          )) : <span className="small faint">Nothing close.</span> : null}
        </form>
      ) : null}
    </Card>
  );
}
