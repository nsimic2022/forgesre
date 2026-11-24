import { AuditPanel, JournalPanel } from "../components/ActivityPanels";
import { AlertPrefs, AppearancePanel, PushPrefs, SeverityColorPrefs } from "../components/AppearancePanel";
import { ForgeAiDocs } from "../components/ForgeAiDocs";
import { ForgeAiSkills } from "../components/ForgeAiSkills";
import { GoldenSet } from "../components/GoldenSet";
import { useRoleRank } from "../lib/me";
import { ForgeAiModels } from "../components/ForgeAiModels";
import { ForgeAiStatus } from "../components/ForgeAiStatus";
import { IntegrationsPanel } from "../components/IntegrationsPanel";
import { MessagePreviews } from "../components/MessagePreviews";
import { BackupPanel, UsersPanel } from "../components/AdminPanels";
import { ChecksPanel } from "../components/ChecksPanel";
import { ContainersPanel } from "../components/ContainersPanel";
import { SyntheticChecks } from "../components/SyntheticChecks";
import { DemoPanel } from "../components/DemoPanel";
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
  const rank = useRoleRank();
  const [data, setData] = useState<SettingsData | null>(null);
  // ?group=checks (command palette, links in docs) opens that group
  const [group, setGroup] = useState(() => new URLSearchParams(window.location.search).get("group") || "general");
  // Ctrl+K → a Settings group while this page is already open: the palette sends the group
  useEffect(() => {
    const pick = (e: Event) => setGroup(String((e as CustomEvent<string>).detail || "general"));
    window.addEventListener("forgesre-settings-group", pick);
    return () => window.removeEventListener("forgesre-settings-group", pick);
  }, []);
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
  // ForgeAI skills and RAG sit right under RCA / AI
  const sections = data ? [...Object.entries(data.groups).flatMap(([k, v]) => (k === "ai" ? [[k, v], ["skills", "ForgeAI skills"], ["rag", "ForgeAI RAG"]] : [[k, v]])), ["messages", "Messages"], ["push", "Web push"], ["appearance", "Appearance"], ["services", "Health"], ["containers", "Containers"], ["journal", "Journal"], ["audit", "Audit"], ["users", "Users"], ["backup", "Backup"], ["demo", "Demo"]] : [];
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
          <Card><span>Settings are for Admins. You can still pick your theme and see the health of every service below{rank >= 2 ? "; as an Engineer you also edit ForgeAI skills and documents here" : ""}.</span></Card>
          {rank >= 2 ? <><ForgeAiSkills /><GoldenSet /><ForgeAiDocs /></> : null}
          <><AppearancePanel /><SeverityColorPrefs /><AlertPrefs /><PushPrefs /></>
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
          <div style={{ flex: "999 1 480px", minWidth: 0, display: "flex", flexDirection: "column", gap: 20 }}>
            {/* ForgeAI: status and models first, the raw settings below them */}
            {group === "ai" ? <><ForgeAiStatus /><ForgeAiModels /></> : null}
            {group === "journal" ? (
              <JournalPanel />
            ) : group === "audit" ? (
              <AuditPanel />
            ) : group === "integrations" ? (
              <IntegrationsPanel rows={data.settings} onSaved={replace} />
            ) : group === "appearance" ? (
              <><AppearancePanel /><SeverityColorPrefs /><AlertPrefs /><PushPrefs admin /></>
            ) : group === "skills" ? (
              <><ForgeAiSkills /><GoldenSet /></>
            ) : group === "rag" ? (
              <ForgeAiDocs />
            ) : group === "push" ? (
              <PushPrefs admin />
            ) : group === "messages" ? (
              <MessagePreviews />
            ) : group === "services" ? (
              <Services />
            ) : group === "containers" ? (
              <ContainersPanel />
            ) : group === "users" ? (
              <UsersPanel />
            ) : group === "backup" ? (
              <BackupPanel />
            ) : group === "demo" ? (
              <DemoPanel />
            ) : (
              <Card title={data.groups[group]} actions={tests.map((t) => <TestButton key={t} component={t} />)}>
                {rows.map((row) => (row.editable ? <SettingField key={row.key} row={row} onSaved={replace} /> : <ReadOnlyRow key={row.key} row={row} />))}
                <span className="small faint">Every change is written to config/forgesre.yml, applied at once and kept in the audit log. Same as <code className="mono">./forgesre settings set KEY VALUE</code>.</span>
              </Card>
            )}
            {group === "checks" ? <><ChecksPanel /><SyntheticChecks /></> : null}
          </div>
        </div>
      ) : null}
    </>
  );
}
