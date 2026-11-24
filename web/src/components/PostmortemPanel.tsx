import { useEffect, useState } from "react";
import { getJson } from "../lib/api";
import { Card } from "./ui";

/** Incident review (postmortem) draft for one incident: preview, copy, download .md. Gaps are marked [to fill]. */
export function PostmortemPanel({ number, onClose }: { number: string; onClose: () => void }) {
  const [text, setText] = useState("");
  const [error, setError] = useState("");
  const [copied, setCopied] = useState(false);
  useEffect(() => {
    getJson<{ markdown: string }>(`/api/v1/incidents/${encodeURIComponent(number)}/postmortem`).then((r) => setText(r.markdown)).catch((e: Error) => setError(e.message));
  }, [number]);
  const copy = () => navigator.clipboard?.writeText(text).then(() => setCopied(true)).catch(() => setError("Copy is blocked here; use Download."));
  const gaps = (text.match(/\[to fill\]/g) ?? []).length;
  return (
    <Card title="Incident review draft" actions={
      <>
        <span className="small faint">{gaps ? `${gaps} gaps to fill in the review` : ""}</span>
        <button className="btn" type="button" onClick={copy} disabled={!text}>{copied ? "Copied" : "Copy Markdown"}</button>
        <a className="btn primary" href={`/api/v1/incidents/${encodeURIComponent(number)}/postmortem?download=1`}>Download .md</a>
        <button className="btn" type="button" onClick={onClose}>Close</button>
      </>
    }>
      {error ? <div className="banner error" role="alert">{error}</div> : null}
      <pre className="mono" style={{ margin: 0, whiteSpace: "pre-wrap", maxHeight: 520, overflow: "auto", background: "var(--s2)", padding: 14, borderRadius: 10, lineHeight: 1.55 }}>{text || "Building the draft…"}</pre>
      <span className="small faint">Built from the facts on this page only. Paste it into your wiki or docs and fill the gaps in the review meeting.</span>
    </Card>
  );
}
