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

// Wide pages: label | value | action in three columns. Narrow cards (Integrations): stacked, so
// nothing is squeezed into a column a few characters wide or drawn over the Save button.
const ROW = { display: "grid", gridTemplateColumns: "minmax(160px, 240px) minmax(0, 1fr) auto", gap: 12, alignItems: "center", padding: "10px 0", borderTop: "1px solid var(--border)" } as const;
const STACK = { display: "flex", flexDirection: "column", gap: 6, padding: "10px 0", borderTop: "1px solid var(--border)", minWidth: 0 } as const;

/** One editable setting: input by kind, Save sends PUT /api/v1/settings/{key}. ``compact`` stacks it for narrow cards. */
export function SettingField({ row, onSaved, compact = false }: { row: SettingRow; onSaved: (row: SettingRow) => void; compact?: boolean }) {
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
    input = <input id={id} className="search" type={row.kind === "int" ? "number" : "text"} value={String(value)} onChange={(e) => setValue(e.target.value)} style={{ width: "100%", minWidth: 0 }} />;
  }
  const label = (
    <label htmlFor={id} className="cell-stack" style={{ minWidth: 0 }}>
      <span>{row.label}</span>
      <span className="mono faint" style={{ fontSize: 11, overflowWrap: "anywhere" }}>{row.key}</span>
    </label>
  );
  const action = (
    <div className="row" style={{ flex: "none" }}>
      {state.kind === "saved" && !dirty ? <Chip tone="ok">Saved</Chip> : null}
      <button className="btn" disabled={!dirty} onClick={save}>Save</button>
    </div>
  );
  if (compact) {
    return (
      <div style={STACK}>
        {label}
        <div className="row" style={{ gap: 8, flexWrap: "nowrap", alignItems: "center" }}>
          <div style={{ flex: row.kind === "bool" ? "none" : "1 1 auto", minWidth: 0 }}>{input}</div>
          {action}
        </div>
        {row.help ? <span className="small faint">{row.help}</span> : null}
        {state.kind === "error" ? <span className="small" role="alert" style={{ color: "var(--crit)" }}>{state.text}</span> : null}
      </div>
    );
  }
  return (
    <div style={ROW}>
      {label}
      <div className="cell-stack" style={{ minWidth: 0 }}>
        {input}
        {row.help ? <span className="small faint">{row.help}</span> : null}
        {state.kind === "error" ? <span className="small" role="alert" style={{ color: "var(--crit)" }}>{state.text}</span> : null}
      </div>
      {action}
    </div>
  );
}

/** A secret (status only, set on the server) or an installer-owned value. ``compact`` stacks it for narrow cards. */
export function ReadOnlyRow({ row, compact = false }: { row: SettingRow; compact?: boolean }) {
  if (compact) {
    return (
      <div style={STACK}>
        <div className="row" style={{ justifyContent: "space-between", gap: 8, flexWrap: "nowrap" }}>
          <span className="cell-stack" style={{ minWidth: 0 }}>
            <span>{row.label}</span>
            <span className="mono faint" style={{ fontSize: 11, overflowWrap: "anywhere" }}>{row.key}</span>
          </span>
          {row.store === "secret" ? <Chip tone={row.set ? "ok" : "warn"}>{row.set ? "Set" : "Not set"}</Chip> : <Chip tone="off">Installer</Chip>}
        </div>
        {row.store === "secret" ? (
          <span className="small muted" style={{ overflowWrap: "anywhere" }}>
            Set on the server: <code className="mono">./forgesre settings set-secret {row.key}</code>
          </span>
        ) : (
          <span className="mono small" style={{ overflowWrap: "anywhere" }}>{shown(row.value) || <span className="faint">—</span>}</span>
        )}
      </div>
    );
  }
  return (
    <div style={ROW}>
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

/** Test connection. The result line is left out when it only repeats ``known`` (text already on the card). */
export function TestButton({ component, label, known = "" }: { component: string; label?: string; known?: string }) {
  const [result, setResult] = useState<{ status: string; why?: string; detail?: string } | null>(null);
  const [busy, setBusy] = useState(false);
  const run = () => {
    setBusy(true);
    postJson<{ status: string; why?: string; detail?: string }>(`/api/v1/settings/test/${component}`, {})
      .then(setResult)
      .catch((err: Error) => setResult({ status: "error", why: err.message }))
      .finally(() => setBusy(false));
  };
  const text = result ? result.why || result.detail || "" : "";
  return (
    <span className="row">
      <button className="btn" onClick={run} disabled={busy}>{busy ? "Testing…" : label ?? `Test ${component}`}</button>
      {result ? <Chip tone={tone(result.status)}>{result.status}</Chip> : null}
      {text && text.trim() !== known.trim() ? <span className="small muted" style={{ flexBasis: "100%", overflowWrap: "anywhere" }}>{text}</span> : null}
    </span>
  );
}

