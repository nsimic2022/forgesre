import { useEffect, useState } from "react";
import { getJson } from "../lib/api";
import { Card, Chip } from "./ui";

type Runtime = {
  chat: { file: string; label: string; size_gb: number; context: string; threads: string; gpu_layers: string; gpu: boolean; url: string; mode: string;
    thinking: boolean; answer_percent: number; think_tokens: number; timeout_s: number };
  embed: { files: { name: string; size_mb: number }[]; model: string; url: string; threads: string };
  dir: string;
  answer_lengths: { task: string; label: string; base: number; now: number }[];
};

function Row({ k, v, note }: { k: string; v: React.ReactNode; note?: string }) {
  return <tr><td className="small muted" style={{ width: "34%" }}>{k}</td><td>{v}</td><td className="small faint">{note}</td></tr>;
}

/**
 * Settings › RCA / AI › Model details: which file each model runs from and how it is started, in one
 * place. Read only; context and threads change in Settings › Server, answer length and thinking
 * room in the settings below, the model itself in the list above or from the chat.
 */
export function ModelDetails() {
  const [r, setR] = useState<Runtime | null>(null);
  const [error, setError] = useState("");
  useEffect(() => void getJson<Runtime>("/api/v1/forgeai/runtime").then(setR).catch((e: Error) => setError(e.message)), []);
  if (error) return <div className="banner error" role="alert">{error}</div>;
  if (!r || !r.chat || !r.embed) return null;
  const c = r.chat;
  return (
    <Card title="Model details">
      <table className="data">
        <tbody>
          <tr><td colSpan={3}><strong>ForgeAI model</strong> <Chip tone={c.mode === "external" ? "info" : "ok"}>{c.mode || "local"}</Chip></td></tr>
          <Row k="Model" v={c.file ? <><span>{c.label}</span> <span className="small mono faint">{c.file}</span></> : <span className="faint">none chosen</span>} note={c.size_gb ? `${c.size_gb} GB in ${r.dir}` : ""} />
          <Row k="Context" v={`${c.context} tokens`} note="FORGESRE_LLM_CTX · Settings › Server" />
          <Row k="CPU threads" v={c.threads} note="FORGESRE_LLM_THREADS · Settings › Server" />
          <Row k="GPU" v={c.gpu ? `on · ${c.gpu_layers} layers` : "off"} note="./forgesre gpu on · FORGESRE_LLM_GPU_LAYERS" />
          <Row k="Thinking" v={c.thinking ? `on · ${c.think_tokens} tokens` : "off"} note="ai.llm.thinking · ai.llm.think_tokens" />
          <Row k="Answer length" v={`${c.answer_percent} %`} note="ai.llm.answer_percent" />
          <Row k="Time limit" v={`${c.timeout_s} s`} note="ai.llm.timeout_seconds (set by the model list)" />
          <Row k="Address" v={<span className="mono small">{c.url || "—"}</span>} />
          <tr><td colSpan={3} style={{ paddingTop: 14 }}><strong>Document search model (ForgeAI RAG)</strong></td></tr>
          <Row k="Model" v={r.embed.model || "—"} note="ai.embed.model (a label; the file below is what runs)" />
          <Row k="File" v={r.embed.files.length ? r.embed.files.map((f) => <span key={f.name} className="mono small">{f.name} · {f.size_mb} MB </span>) : <span className="faint">none: ./forgesre fetch-llm</span>} note={`${r.dir}/embed`} />
          <Row k="CPU threads" v={r.embed.threads} note="FORGESRE_EMBED_THREADS · Settings › Server" />
          <Row k="Address" v={<span className="mono small">{r.embed.url || "off"}</span>} note="ai.embed.url" />
        </tbody>
      </table>
      <details>
        <summary className="small" style={{ cursor: "pointer" }}>Answer length per task (tokens)</summary>
        <table className="data" style={{ marginTop: 8 }}>
          <thead><tr><th>Task</th><th>Built-in</th><th>Now</th></tr></thead>
          <tbody>{r.answer_lengths.map((a) => <tr key={a.task}><td>{a.label}</td><td>{a.base}</td><td>{a.now}</td></tr>)}</tbody>
        </table>
      </details>
    </Card>
  );
}
