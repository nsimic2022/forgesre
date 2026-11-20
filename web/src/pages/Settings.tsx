import { AppearancePanel } from "../components/AppearancePanel";
import { IntegrationsPanel } from "../components/IntegrationsPanel";
import { MessagePreviews } from "../components/MessagePreviews";
import { BackupPanel, UsersPanel } from "../components/AdminPanels";
import { ChecksPanel } from "../components/ChecksPanel";
import { useEffect, useState } from "react";
import { Card, Chip } from "../components/ui";
import { ApiError, getJson, type Doctor, type SettingRow, type SettingsData } from "../lib/api";
import { ReadOnlyRow, SettingField, TestButton, tone } from "../components/SettingControls";

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
  const sections = data ? [...Object.entries(data.groups), ["messages", "Messages and templates"], ["appearance", "Appearance"], ["services", "Services and health"], ["users", "Users and roles"], ["backup", "Backup, import, export"]] : [];
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
          <Card><span>Settings are for Admins. You can still pick your theme and see the health of every service below.</span></Card>
          <AppearancePanel />
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
          </nav>
          <div style={{ flex: "999 1 640px", minWidth: 0, display: "flex", flexDirection: "column", gap: 20 }}>
            {group === "integrations" ? (
              <IntegrationsPanel rows={data.settings} onSaved={replace} />
            ) : group === "appearance" ? (
              <AppearancePanel />
            ) : group === "messages" ? (
              <MessagePreviews />
            ) : group === "services" ? (
              <Services />
            ) : group === "users" ? (
              <UsersPanel />
            ) : group === "backup" ? (
              <BackupPanel />
            ) : (
              <Card title={data.groups[group]} actions={tests.map((t) => <TestButton key={t} component={t} />)}>
                {rows.map((row) => (row.editable ? <SettingField key={row.key} row={row} onSaved={replace} /> : <ReadOnlyRow key={row.key} row={row} />))}
                <span className="small faint">Every change is written to config/forgesre.yml, applied at once and kept in the audit log. Same as <code className="mono">./forgesre settings set KEY VALUE</code>.</span>
              </Card>
            )}
            {group === "checks" ? <ChecksPanel /> : null}
          </div>
        </div>
      ) : null}
    </>
  );
}
