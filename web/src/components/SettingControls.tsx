import { useState } from "react";
import { postJson, putJson, type SettingRow } from "../lib/api";
import type { Tone } from "../lib/format";
import { Chip } from "./ui";

// Setting rows and the Test button, shared by Settings and its Integrations cards.
const STATUS_TONE: Record<string, Tone> = { ok: "ok", healthy: "ok", disabled: "off", skipped: "off", warning: "warn", degraded: "warn", error: "crit", down: "crit" };
export const tone = (status: string): Tone => STATUS_TONE[status.toLowerCase()] ?? "warn";

function shown(value: unknown): string {
  if (Array.isArray(value)) return value.join(", ");
  if (value === null || value === undefined || value === "") return "";
  return String(value);
}

/** One editable setting: input by kind, Save sends PUT /api/v1/settings/{key}. */
export function SettingField({ row, onSaved }: { row: SettingRow; onSaved: (row: SettingRow) => void }) {
  const initial = row.value ?? row.effective;
  const [value, setValue] = useState<string | boolean>(row.kind === "bool" ? Boolean(initial) : shown(initial));
  const [state, setState] = useState<{ kind: "idle" | "saved" | "error"; text?: string }>({ kind: "idle" });
  const dirty = row.kind === "bool" ? value !== Boolean(initial) : value !== shown(initial);
  const save = () =>
    putJson<SettingRow>(`/api/v1/settings/${row.key}`, { value })
      .then((next) => (setState({ kind: "saved" }), onSaved(next)))
      .catch((err: Error) => setState({ kind: "error", text: err.message }));
  const id = `setting-${row.key}`;
  let input;
  if (row.kind === "bool") {
    input = <input id={id} type="checkbox" checked={Boolean(value)} onChange={(e) => setValue(e.target.checked)} style={{ width: 18, height: 18, accentColor: "var(--accent)" }} />;
  } else if (row.kind === "enum") {
    input = (
      <select id={id} className="search" value={String(value)} onChange={(e) => setValue(e.target.value)}>
        {(row.choices ?? []).map((c) => <option key={c} value={c}>{c}</option>)}
      </select>
    );
  } else {
    input = <input id={id} className="search" type={row.kind === "int" ? "number" : "text"} value={String(value)} onChange={(e) => setValue(e.target.value)} style={{ width: "100%" }} />;
  }
  return (
    <div style={{ display: "grid", gridTemplateColumns: "minmax(160px, 240px) minmax(0, 1fr) auto", gap: 12, alignItems: "center", padding: "10px 0", borderTop: "1px solid var(--border)" }}>
      <label htmlFor={id} className="cell-stack">
        <span>{row.label}</span>
        <span className="mono faint" style={{ fontSize: 11 }}>{row.key}</span>
      </label>
      <div className="cell-stack">
        {input}
        {row.help ? <span className="small faint">{row.help}</span> : null}
        {state.kind === "error" ? <span className="small" role="alert" style={{ color: "var(--crit)" }}>{state.text}</span> : null}
      </div>
      <div className="row">
        {state.kind === "saved" && !dirty ? <Chip tone="ok">Saved</Chip> : null}
        <button className="btn" disabled={!dirty} onClick={save}>Save</button>
      </div>
    </div>
  );
}

export function ReadOnlyRow({ row }: { row: SettingRow }) {
  return (
    <div style={{ display: "grid", gridTemplateColumns: "minmax(160px, 240px) minmax(0, 1fr) auto", gap: 12, alignItems: "center", padding: "10px 0", borderTop: "1px solid var(--border)" }}>
      <div className="cell-stack">
        <span>{row.label}</span>
        <span className="mono faint" style={{ fontSize: 11 }}>{row.key}</span>
      </div>
      {row.store === "secret" ? (
        <span className="small muted">
          Set on the server: <code className="mono">./forgesre settings set-secret {row.key}</code>
        </span>
      ) : (
        <span className="mono">{shown(row.value) || <span className="faint">—</span>}</span>
      )}
      {row.store === "secret" ? <Chip tone={row.set ? "ok" : "warn"}>{row.set ? "Set" : "Not set"}</Chip> : <Chip tone="off">Installer</Chip>}
    </div>
  );
}

export function TestButton({ component, label }: { component: string; label?: string }) {
  const [result, setResult] = useState<{ status: string; why?: string; detail?: string } | null>(null);
  const [busy, setBusy] = useState(false);
  const run = () => {
    setBusy(true);
    postJson<{ status: string; why?: string; detail?: string }>(`/api/v1/settings/test/${component}`, {})
      .then(setResult)
      .catch((err: Error) => setResult({ status: "error", why: err.message }))
      .finally(() => setBusy(false));
  };
  return (
    <span className="row">
      <button className="btn" onClick={run} disabled={busy}>{busy ? "Testing…" : label ?? `Test ${component}`}</button>
      {result ? <Chip tone={tone(result.status)}>{result.status}</Chip> : null}
      {result?.why || result?.detail ? <span className="small muted">{result.why || result.detail}</span> : null}
    </span>
  );
}

