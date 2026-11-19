import { useEffect, useState } from "react";
import { Card, Chip } from "../components/ui";
import { ApiError, getJson, postJson, putJson, type Doctor, type SettingRow, type SettingsData } from "../lib/api";
import type { Tone } from "../lib/format";

const STATUS_TONE: Record<string, Tone> = { ok: "ok", healthy: "ok", disabled: "off", skipped: "off", warning: "warn", degraded: "warn", error: "crit", down: "crit" };
const tone = (status: string): Tone => STATUS_TONE[status.toLowerCase()] ?? "warn";

function shown(value: unknown): string {
  if (Array.isArray(value)) return value.join(", ");
  if (value === null || value === undefined || value === "") return "";
  return String(value);
}

/** One editable setting: input by kind, Save sends PUT /api/v1/settings/{key}. */
function SettingField({ row, onSaved }: { row: SettingRow; onSaved: (row: SettingRow) => void }) {
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

function ReadOnlyRow({ row }: { row: SettingRow }) {
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

function TestButton({ component }: { component: string }) {
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
      <button className="btn" onClick={run} disabled={busy}>{busy ? "Testing…" : `Test ${component}`}</button>
      {result ? <Chip tone={tone(result.status)}>{result.status}</Chip> : null}
      {result?.why || result?.detail ? <span className="small muted">{result.why || result.detail}</span> : null}
    </span>
  );
}

function Services() {
  const [doctor, setDoctor] = useState<Doctor | null>(null);
  const [error, setError] = useState("");
  useEffect(() => {
    getJson<Doctor>("/api/v1/system/doctor").then(setDoctor).catch((e: Error) => setError(e.message));
  }, []);
  return (
    <Card title="Services and integrations" actions={doctor ? <Chip tone={doctor.overall === "HEALTHY" ? "ok" : "warn"}>{doctor.overall}</Chip> : null}>
      {error ? <div className="banner error" role="alert">{error}</div> : null}
      {doctor
        ? Object.entries(doctor.components).map(([name, c]) => (
            <div key={name} className="row" style={{ justifyContent: "space-between", borderTop: "1px solid var(--border)", paddingTop: 8 }}>
              <div className="cell-stack">
                <span style={{ fontWeight: 600 }}>{c.label || name}</span>
                {c.why || c.detail ? <span className="small muted">{c.why || c.detail}</span> : null}
              </div>
              <Chip tone={tone(c.status)}>{c.status}</Chip>
            </div>
          ))
        : <span className="muted">Checking…</span>}
      <span className="small faint">Same checks as <code className="mono">./forgesre doctor</code>. Logs and restarts: <code className="mono">./forgesre logs core</code>, <code className="mono">./forgesre restart</code>.</span>
    </Card>
  );
}

export function SettingsPage() {
  const [data, setData] = useState<SettingsData | null>(null);
  const [group, setGroup] = useState("general");
  const [error, setError] = useState("");
  const [forbidden, setForbidden] = useState(false);
  useEffect(() => {
    getJson<SettingsData>("/api/v1/settings")
      .then(setData)
      .catch((err: Error) => (err instanceof ApiError && err.status === 403 ? setForbidden(true) : setError(err.message)));
  }, []);
  const replace = (next: SettingRow) => setData((d) => (d ? { ...d, settings: d.settings.map((r) => (r.key === next.key ? next : r)) } : d));
  const rows = data?.settings.filter((r) => r.group === group) ?? [];
  const tests = Array.from(new Set(rows.map((r) => r.test).filter(Boolean)));
  const sections = data ? [...Object.entries(data.groups), ["services", "Services and health"]] : [];
  return (
    <>
      <header className="page-head">
        <div>
          <h1>Settings</h1>
          <span className="muted">Values written by the installer, editable here · secrets can be replaced on the server but never shown</span>
        </div>
      </header>
      {error ? <div className="banner error" role="alert">{error}</div> : null}
      {forbidden ? (
        <>
          <Card><span>Settings are for Admins. You can still see the health of every service below.</span></Card>
          <Services />
        </>
      ) : null}
      {data ? (
        <div className="cols">
          <nav aria-label="Settings sections" className="card" style={{ flex: "1 1 220px", maxWidth: 260, gap: 2, padding: 10 }}>
            {sections.map(([key, title]) => (
              <button key={key} className="nav-link" aria-current={group === key ? "page" : undefined} onClick={() => setGroup(key)} style={{ background: group === key ? "var(--s2)" : "none", border: 0, cursor: "pointer", color: group === key ? "var(--text)" : "var(--muted)", textAlign: "left", width: "100%" }}>
                {title}
              </button>
            ))}
            <a className="nav-link" href="/admin">Users, roles, backup (classic)</a>
          </nav>
          <div style={{ flex: "999 1 640px", minWidth: 0, display: "flex", flexDirection: "column", gap: 20 }}>
            {group === "services" ? (
              <Services />
            ) : (
              <Card title={data.groups[group]} actions={tests.map((t) => <TestButton key={t} component={t} />)}>
                {rows.map((row) => (row.editable ? <SettingField key={row.key} row={row} onSaved={replace} /> : <ReadOnlyRow key={row.key} row={row} />))}
                <span className="small faint">Every change is written to config/forgesre.yml, applied at once and kept in the audit log. Same as <code className="mono">./forgesre settings set KEY VALUE</code>.</span>
              </Card>
            )}
          </div>
        </div>
      ) : null}
    </>
  );
}
