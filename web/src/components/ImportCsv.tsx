import { useState } from "react";
import { postJson } from "../lib/api";
import { waitForJob } from "../lib/jobs";
import { Card, Chip } from "./ui";

type Row = { line: number; hostname: string; ip: string; type: string; asset_id: string; action: "create" | "exists" | "error"; reason: string };
type Preview = { rows: Row[]; counts: { create: number; exists: number; error: number }; ignored_columns: string[]; columns: string[] };
type Result = { created: string[]; failed: { line: number; hostname: string; reason: string }[] };

const ACTION: Record<Row["action"], { label: string; tone: "ok" | "off" | "crit" }> = {
  create: { label: "Add", tone: "ok" }, exists: { label: "Already there", tone: "off" }, error: { label: "Problem", tone: "crit" },
};

/** Inventory › Import CSV: check the file first (nothing is written), then add the accepted rows
 *  in a background job. Existing assets are skipped, never changed. */
export function ImportCsv({ onClose, onImported }: { onClose: () => void; onImported: (count: number) => void }) {
  const [text, setText] = useState("");
  const [name, setName] = useState("");
  const [preview, setPreview] = useState<Preview | null>(null);
  const [busy, setBusy] = useState("");
  const [error, setError] = useState("");
  const [result, setResult] = useState<Result | null>(null);

  const pick = async (file: File | undefined) => {
    if (!file) return;
    setName(file.name);
    setText(await file.text());
    setPreview(null);
    setResult(null);
  };
  const check = async () => {
    setBusy("check");
    setError("");
    try {
      setPreview(await postJson<Preview>("/api/v1/assets/import/preview", { csv: text }));
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setBusy("");
    }
  };
  const run = async () => {
    setBusy("import");
    setError("");
    try {
      const started = await postJson<{ job: number }>("/api/v1/assets/import", { csv: text });
      const job = await waitForJob<Result>(started.job, { tries: 400 });
      if (job.status === "error" || !job.result) setError(job.error || "The import failed.");
      else {
        setResult(job.result);
        setPreview(null);
        onImported(job.result.created.length);
      }
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setBusy("");
    }
  };

  return (
    <Card title="Import CSV" actions={<button className="btn" type="button" onClick={onClose}>Close</button>}>
      <p className="small muted" style={{ margin: 0 }}>
        One asset per line. The first line names the columns; <span className="mono">hostname</span> is required, comma or semicolon both work.
        Assets already in the inventory (same id or IP) are skipped, never changed. SNMP communities and passwords are not read from CSV: set them on the asset afterwards.{" "}
        <a href="/api/v1/assets/import/template">Download a template</a>.
      </p>
      <div className="row">
        <label className="btn">
          Choose file
          <input type="file" accept=".csv,text/csv,text/plain" aria-label="CSV file" style={{ display: "none" }} onChange={(e) => void pick(e.target.files?.[0])} />
        </label>
        <span className="small muted">{name || "or paste below"}</span>
      </div>
      <textarea aria-label="CSV text" rows={5} className="mono" style={{ width: "100%" }} value={text}
        placeholder={"hostname,ip,type,site,rack\nweb-01,10.0.0.11,Linux Server,DC1,A07"}
        onChange={(e) => (setText(e.target.value), setPreview(null), setResult(null))} />
      <div className="row">
        <button className="btn" type="button" disabled={!text.trim() || !!busy} onClick={() => void check()}>{busy === "check" ? "Checking…" : "Check file"}</button>
        {preview && preview.counts.create > 0 ? (
          <button className="btn primary" type="button" disabled={!!busy} onClick={() => void run()}>
            {busy === "import" ? "Adding…" : `Add ${preview.counts.create} asset${preview.counts.create === 1 ? "" : "s"}`}
          </button>
        ) : null}
      </div>
      {error ? <div className="banner error" role="alert">{error}</div> : null}
      {result ? (
        <div className="banner" role="status" style={{ background: result.failed.length ? "var(--warn-bg)" : "var(--ok-bg)" }}>
          Added {result.created.length} asset{result.created.length === 1 ? "" : "s"}.
          {result.failed.map((f) => <div key={`${f.line}-${f.hostname}`} className="small">Line {f.line} {f.hostname}: {f.reason}</div>)}
          {result.created.length ? <div className="small">Open each one and run the monitoring check to see that ForgeSRE really watches it.</div> : null}
        </div>
      ) : null}
      {preview ? (
        <>
          <span className="small muted">
            {preview.counts.create} to add · {preview.counts.exists} already there · {preview.counts.error} with problems
            {preview.ignored_columns.length ? ` · ignored columns: ${preview.ignored_columns.join(", ")}` : ""}
          </span>
          <div className="table-wrap">
            <table className="data">
              <thead><tr><th style={{ width: 56 }}>Line</th><th>Hostname</th><th>IP</th><th>Type</th><th style={{ width: 120 }}>Result</th><th>Why</th></tr></thead>
              <tbody>
                {preview.rows.map((r) => (
                  <tr key={r.line}>
                    <td className="faint">{r.line}</td>
                    <td>{r.hostname}</td>
                    <td className="mono muted">{r.ip}</td>
                    <td>{r.type}</td>
                    <td><Chip tone={ACTION[r.action].tone}>{ACTION[r.action].label}</Chip></td>
                    <td className="small muted">{r.reason}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </>
      ) : null}
    </Card>
  );
}
