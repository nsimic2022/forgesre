import { useEffect, useRef, useState, type FormEvent } from "react";
import { ApiError, getJson, postJson } from "../lib/api";
import { Card, Chip } from "./ui";

type Installed = { name: string; size_gb: number; parts: number; complete: boolean; valid: boolean; active: boolean; catalog: string };
type CatalogItem = { id: string; name: string; size_gb: number; ram_gb: number; timeout_s: number; note: string; files: string[]; family?: string; thinking?: boolean };
type Download = { id: number; status: string; error: string; label?: string; done_bytes?: number; total_bytes?: number; file?: string; cancel?: boolean };
type Data = { dir: string; writable: boolean; disk_free_gb: number; ram_gb: number; active: string; installed: Installed[]; catalog: CatalogItem[]; downloads: Download[] };

async function send(url: string, method: string): Promise<unknown> {
  const res = await fetch(url, { method, credentials: "same-origin" });
  const body = await res.json().catch(() => ({}));
  if (!res.ok) throw new ApiError(res.status, (body as { detail?: string }).detail ?? res.statusText);
  return body;
}

/** Settings › RCA / AI: the models on this appliance, the catalog, a Hugging Face link, file import. */
export function ForgeAiModels() {
  const [data, setData] = useState<Data | null>(null);
  const [url, setUrl] = useState("");
  const [note, setNote] = useState("");
  const [error, setError] = useState("");
  const [upload, setUpload] = useState<number | null>(null);
  const timer = useRef<number | undefined>(undefined);
  const load = () => getJson<Data>("/api/v1/llm/models").then((d) => setData(d && d.catalog ? d : null)).catch((e: Error) => setError(e.message));
  useEffect(() => void load(), []);
  const running = data?.downloads.some((d) => d.status === "pending" || d.status === "running");
  useEffect(() => {
    if (!running) return;
    timer.current = window.setTimeout(load, 3000);
    return () => window.clearTimeout(timer.current);
  }, [data]);
  const act = (p: Promise<unknown>, done: string) => {
    setError("");
    p.then(() => (setNote(done), load())).catch((e: Error) => setError(e.message));
  };
  const download = (body: object) => act(postJson("/api/v1/llm/models/download", body), "Download started; it runs in the background and survives closing this page.");
  const fromLink = (e: FormEvent) => (e.preventDefault(), url.trim() ? download({ url: url.trim() }) : undefined);
  const importFile = (file: File) => {
    setError("");
    const form = new FormData();
    form.append("file", file);
    const xhr = new XMLHttpRequest();
    xhr.open("POST", "/api/v1/llm/models/upload");
    xhr.upload.onprogress = (ev) => ev.lengthComputable && setUpload(Math.round((ev.loaded / ev.total) * 100));
    xhr.onload = () => {
      setUpload(null);
      if (xhr.status >= 300) setError((() => { try { return JSON.parse(xhr.responseText).detail; } catch { return xhr.statusText; } })());
      else (setNote(`${file.name} imported.`), load());
    };
    xhr.onerror = () => (setUpload(null), setError("The upload failed."));
    xhr.send(form);
  };
  const installedIds = new Set((data?.installed ?? []).map((m) => m.catalog).filter(Boolean));
  return (
    <Card title="ForgeAI models">
      {data ? (
        <span className="small faint">
          This VM: {data.ram_gb} GB RAM · {data.disk_free_gb} GB free disk. Switching takes a few seconds: llama.cpp restarts with the new model, then
          loads it (up to a minute). Only the active model uses RAM.
        </span>
      ) : null}
      {data && !data.writable ? <div className="banner" role="note">{data.dir} is read-only for Core. Run ./forgesre update once to apply the new mount.</div> : null}
      {error ? <span className="small" role="alert" style={{ color: "var(--crit)" }}>{error}</span> : null}
      {note ? <span className="small" style={{ color: "var(--ok)" }}>{note}</span> : null}

      <strong className="small">On this appliance</strong>
      {data && !data.installed.length ? <span className="muted">No model yet. Download one below.</span> : null}
      {(data?.installed ?? []).map((m) => (
        <div key={m.name} className="row" style={{ justifyContent: "space-between", borderTop: "1px solid var(--border)", paddingTop: 8 }}>
          <div className="cell-stack" style={{ gap: 2 }}>
            <span className="mono small">{m.name}</span>
            <span className="small faint">{m.size_gb} GB{m.parts > 1 ? ` in ${m.parts} files` : ""}{m.valid ? "" : " · incomplete or not a GGUF"}</span>
          </div>
          <div className="row" style={{ gap: 6 }}>
            {m.active ? <Chip tone="ok">Active</Chip> : (
              <>
                <button className="btn" type="button" disabled={!m.valid} onClick={() => act(postJson(`/api/v1/llm/models/${encodeURIComponent(m.name)}/activate`, {}), `${m.name} is now active; llama.cpp is restarting with it.`)}>Use this model</button>
                <button className="btn" type="button" onClick={() => window.confirm(`Delete ${m.name}?`) && act(send(`/api/v1/llm/models/${encodeURIComponent(m.name)}`, "DELETE"), `${m.name} deleted.`)}>Delete</button>
              </>
            )}
          </div>
        </div>
      ))}

      {(data?.downloads ?? []).filter((d) => d.status === "pending" || d.status === "running" || d.status === "error").slice(0, 3).map((d) => {
        const pct = d.total_bytes ? Math.round(((d.done_bytes ?? 0) / d.total_bytes) * 100) : 0;
        return (
          <div key={d.id} className="cell-stack" style={{ gap: 4, borderTop: "1px solid var(--border)", paddingTop: 8 }}>
            <div className="row" style={{ justifyContent: "space-between" }}>
              <span className="small">{d.label} · {d.status === "error" ? `failed: ${d.error}` : d.status === "pending" ? "waiting" : `${pct}% (${((d.done_bytes ?? 0) / 1e9).toFixed(1)} of ${((d.total_bytes ?? 0) / 1e9).toFixed(1)} GB)`}</span>
              {d.status === "pending" || d.status === "running" ? <button className="btn" type="button" onClick={() => act(postJson(`/api/v1/llm/downloads/${d.id}/cancel`, {}), "Stopping; the part already fetched is kept, Download again resumes.")}>Stop</button> : null}
            </div>
            {d.status === "running" ? <div style={{ height: 6, background: "var(--s3)", borderRadius: 3 }}><div style={{ width: `${pct}%`, height: 6, background: "var(--accent)", borderRadius: 3 }} /></div> : null}
          </div>
        );
      })}

      <strong className="small">Download from Hugging Face</strong>
      {(data?.catalog ?? []).map((c, i, all) => {
        const tight = data && data.ram_gb > 0 && c.ram_gb > data.ram_gb;
        const head = c.family && c.family !== all[i - 1]?.family;
        return (
          <div key={c.id} className="cell-stack" style={{ gap: 4 }}>
          {head ? <span className="small" style={{ marginTop: 8 }}><strong>{c.family}</strong> <span className="faint">· sizes from smaller to stronger; operations-tuned, not a thinking model (thinking is turned off when you use it)</span></span> : null}
          <div className="row" style={{ justifyContent: "space-between", borderTop: "1px solid var(--border)", paddingTop: 8 }}>
            <div className="cell-stack" style={{ gap: 2 }}>
              <span>{c.name}</span>
              <span className="small faint">{c.size_gb} GB · needs about {c.ram_gb} GB RAM · timeout {c.timeout_s} s · {c.note}</span>
              {tight ? <span className="small" style={{ color: "var(--warn)" }}>More RAM than this VM has.</span> : null}
            </div>
            {installedIds.has(c.id) ? <Chip tone="off">Installed</Chip> : <button className="btn" type="button" disabled={Boolean(running)} onClick={() => download({ catalog_id: c.id })}>Download</button>}
          </div>
          </div>
        );
      })}
      <form className="row" style={{ gap: 6 }} onSubmit={fromLink}>
        <input className="search" style={{ flex: "1 1 360px", minWidth: 0 }} aria-label="Hugging Face link" placeholder="https://huggingface.co/<org>/<repo>/resolve/main/<file>.gguf" value={url} onChange={(e) => setUrl(e.target.value)} />
        <button className="btn" type="submit" disabled={Boolean(running) || !url.trim()}>Download link</button>
      </form>
      <span className="small faint">Any Instruct GGUF works; Q4_K_M is a good size for CPU. The first part of a split model brings the other parts.</span>

      <strong className="small">Import a file</strong>
      <label className="row small" style={{ gap: 8 }}>
        <input type="file" accept=".gguf" aria-label="GGUF file" disabled={upload !== null} onChange={(e) => e.target.files?.[0] && importFile(e.target.files[0])} />
        {upload !== null ? <span>Uploading {upload}%…</span> : null}
      </label>
      <span className="small faint">For a VM without internet: a .gguf from your computer. Large files are quicker copied into data/models on the VM (scp); they show up here.</span>
    </Card>
  );
}
