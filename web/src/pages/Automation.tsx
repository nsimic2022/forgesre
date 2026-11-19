import { useEffect, useState, type FormEvent } from "react";
import { Card, Chip } from "../components/ui";
import { getJson, postJson, type Automation, type PolicyStep, type Simulation } from "../lib/api";
import { clock } from "../lib/format";

type Tab = "rules" | "escalation" | "playbooks" | "maintenance";
const TABS: { key: Tab; label: string }[] = [
  { key: "rules", label: "Routing rules" },
  { key: "escalation", label: "Escalation" },
  { key: "playbooks", label: "Playbooks" },
  { key: "maintenance", label: "Maintenance windows" },
];
// Classic pages that still do the editing.
const EDIT: Record<Tab, string> = { rules: "/playrules", escalation: "/escalation", playbooks: "/playbooks", maintenance: "/assets" };

function condition(c: Automation["rules"][number]["condition"]): string {
  const base = c.alertname ?? "any alert";
  return c.metric && c.operator && c.value !== undefined ? `${base} and ${c.metric} ${c.operator} ${c.value}` : base;
}

function Steps({ steps }: { steps: PolicyStep[] }) {
  return (
    <ol style={{ listStyle: "none", margin: 0, padding: 0 }}>
      {steps.map((s, i) => (
        <li key={i} style={{ display: "grid", gridTemplateColumns: "110px minmax(0,1fr) auto", gap: 10, padding: "8px 0", borderTop: "1px solid var(--border)", alignItems: "center" }}>
          <span className="mono faint">{s.after_minutes === 0 ? "at once" : `+${s.after_minutes} min`}</span>
          <span>{s.level ? `L${s.level} · ` : ""}{s.target}</span>
          <Chip tone="info">{s.channel}</Chip>
        </li>
      ))}
    </ol>
  );
}

function Simulator() {
  const [form, setForm] = useState({ alertname: "HighCPU", asset: "", value: "", at: "" });
  const [result, setResult] = useState<Simulation | null>(null);
  const [error, setError] = useState("");
  const run = (e: FormEvent) => {
    e.preventDefault();
    postJson<Simulation>("/api/v1/automation/simulate", form)
      .then((r) => (setResult(r), setError("")))
      .catch((err: Error) => setError(err.message));
  };
  const field = (key: keyof typeof form, label: string, placeholder: string, type = "text") => (
    <label className="cell-stack small faint" style={{ flex: "1 1 160px" }}>
      {label}
      <input className="search" style={{ minWidth: 0 }} type={type} value={form[key]} placeholder={placeholder} onChange={(e) => setForm({ ...form, [key]: e.target.value })} />
    </label>
  );
  return (
    <Card title="Try an alarm">
      <form onSubmit={run} className="cell-stack" style={{ gap: 12 }}>
        <div className="row" style={{ alignItems: "flex-end" }}>
          {field("alertname", "Alert", "HighCPU")}
          {field("asset", "Asset (hostname, id or IP)", "db-ora-02")}
          {field("value", "Value", "96")}
          {field("at", "Time (empty = now)", "", "datetime-local")}
          <button className="btn primary" type="submit">Run test</button>
        </div>
      </form>
      {error ? <div className="banner error" role="alert">{error}</div> : null}
      {result ? (
        <div className="cell-stack" aria-live="polite">
          <div className="row">
            <Chip tone={result.outcome === "muted" ? "off" : "high"}>{result.outcome === "muted" ? "Muted, nobody notified" : "Incident opens"}</Chip>
            {result.playbook ? <Chip tone="info">Playbook {result.playbook}</Chip> : null}
          </div>
          <ol style={{ margin: 0, paddingLeft: 20 }}>
            {result.trace.map((t, i) => <li key={i}>{t}</li>)}
          </ol>
          {result.steps.length ? <Steps steps={result.steps} /> : null}
        </div>
      ) : (
        <span className="small muted">Uses the same matching as real alerts. Nothing is created and nobody is notified.</span>
      )}
    </Card>
  );
}

export function AutomationPage() {
  const [data, setData] = useState<Automation | null>(null);
  const [tab, setTab] = useState<Tab>("rules");
  const [error, setError] = useState("");
  useEffect(() => {
    getJson<Automation>("/api/v1/automation").then(setData).catch((e: Error) => setError(e.message));
  }, []);
  const p = data?.pipeline;
  const steps: [string, string, string][] = p
    ? [
        ["Collect", "Alertmanager · Zabbix · LibreNMS · syslog", `${p.incidents_today} incidents today`],
        ["Filter noise", "Maintenance windows", `${p.muted_today} muted`],
        ["Route", "Rules pick playbook and escalation", `${p.routed_today} matched a rule`],
        ["Escalate and notify", "Policies, on-call, channels", `${p.notifications_today} messages sent`],
      ]
    : [];
  return (
    <>
      <header className="page-head">
        <div>
          <h1>Automation</h1>
          <span className="muted">What happens between an alarm and someone’s phone</span>
        </div>
        {data?.can_edit ? <a className="btn" href={EDIT[tab]}>Edit in classic UI</a> : null}
      </header>
      {error ? <div className="banner error" role="alert">{error}</div> : null}
      {p ? (
        <div className="row" style={{ alignItems: "stretch" }} role="list" aria-label="Pipeline">
          {steps.map(([title, detail, count], i) => (
            <div key={title} role="listitem" className="card" style={{ flex: "1 1 200px", gap: 4, padding: 14 }}>
              <span style={{ fontWeight: 650 }}>{i + 1} · {title}</span>
              <span className="small muted">{detail}</span>
              <span style={{ fontWeight: 600 }}>{count}</span>
            </div>
          ))}
        </div>
      ) : null}
      <div className="tabs" role="tablist" aria-label="Automation sections">
        {TABS.map((t) => (
          <button key={t.key} role="tab" className="tab" aria-selected={tab === t.key} onClick={() => setTab(t.key)}>
            {t.label}
          </button>
        ))}
      </div>
      {data && tab === "rules" ? (
        <Card title="Routing rules · first match wins" actions={<span className="small faint">Asset-specific rules are tried before global ones</span>}>
          <div className="table-wrap">
            <table className="data" style={{ minWidth: 860 }}>
              <thead>
                <tr><th style={{ width: 160 }}>Rule</th><th>When the alarm is</th><th style={{ width: 90 }}>Severity</th><th style={{ width: 150 }}>Playbook</th><th style={{ width: 160 }}>Escalation</th><th style={{ width: 90 }}>Hits 30 d</th><th style={{ width: 70 }}>On</th></tr>
              </thead>
              <tbody>
                {data.rules.map((r) => (
                  <tr key={r.id}>
                    <td><div className="cell-stack"><span style={{ fontWeight: 600 }}>{r.name}</span><span className="small muted">{r.description}</span></div></td>
                    <td className="mono">{condition(r.condition)}</td>
                    <td>{r.severity}</td>
                    <td>{r.playbook ?? <span className="faint">—</span>}</td>
                    <td>{r.policy ?? <span className="faint">—</span>}</td>
                    <td className="muted">{r.hits_30d}</td>
                    <td><Chip tone={r.enabled ? "ok" : "off"}>{r.enabled ? "On" : "Off"}</Chip></td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </Card>
      ) : null}
      {data && tab === "escalation" ? (
        <div className="cols">
          {data.policies.map((policy) => (
            <Card key={policy.id} title={policy.name} actions={<span className="small faint">{policy.rules} rules</span>}>
              <Steps steps={policy.steps} />
            </Card>
          ))}
        </div>
      ) : null}
      {data && tab === "playbooks" ? (
        <div className="cols">
          {data.playbooks.map((book) => (
            <Card key={book.id} title={book.name} actions={<span className="small faint">{book.rules} rules</span>}>
              {book.description ? <span className="small muted">{book.description}</span> : null}
              <ol style={{ margin: 0, paddingLeft: 20 }}>{book.steps.map((s, i) => <li key={i}>{s}</li>)}</ol>
            </Card>
          ))}
        </div>
      ) : null}
      {data && tab === "maintenance" ? (
        <Card title="Maintenance windows" actions={<span className="small faint">Alarms on these assets are muted while a window is open</span>}>
          {data.maintenance.length === 0 ? <span className="muted">No current or upcoming windows.</span> : null}
          {data.maintenance.map((w, i) => (
            <div key={i} className="row" style={{ justifyContent: "space-between", borderTop: "1px solid var(--border)", paddingTop: 8 }}>
              <div className="cell-stack"><span style={{ fontWeight: 600 }}>{w.asset}</span><span className="small muted">{w.summary}</span></div>
              <span className="row small">
                {clock(w.starts_at)} – {clock(w.ends_at)}
                <Chip tone={w.active ? "warn" : "off"}>{w.active ? "Active" : "Planned"}</Chip>
              </span>
            </div>
          ))}
        </Card>
      ) : null}
      <Simulator />
    </>
  );
}
