import { useEffect, useState, type FormEvent } from "react";
import { KnownErrorsCard } from "../components/KnownErrors";
import { GoldenSet } from "../components/GoldenSet";
import { usePaged } from "../components/Pager";
import { MaintenanceForm, PlaybookForm, PolicyForm, RemoveButton, RuleForm, ruleBody, ruleDraft } from "../components/AutomationEditors";
import { ForgeAiDocs } from "../components/ForgeAiDocs";
import { ForgeAiSkills } from "../components/ForgeAiSkills";
import { NoiseFilters } from "../components/NoiseFilters";
import { Card, Chip } from "../components/ui";
import { getJson, postJson, putJson, type Automation, type PolicyStep, type Simulation } from "../lib/api";
import { clock, tierLabel } from "../lib/format";

type Tab = "filters" | "rules" | "escalation" | "playbooks" | "maintenance" | "known" | "forgeai";
const TABS: { key: Tab; label: string }[] = [
  { key: "filters", label: "Noise filters" },
  { key: "rules", label: "Routing rules" },
  { key: "escalation", label: "Escalation" },
  { key: "playbooks", label: "Playbooks" },
  { key: "maintenance", label: "Maintenance windows" },
  { key: "known", label: "Known errors" },
  { key: "forgeai", label: "ForgeAI skills" },
];
// Classic pages that still do the editing.
type Rule = Automation["rules"][number];
type Book = Automation["playbooks"][number];
type Policy = Automation["policies"][number];

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
          <span>{s.level ? `${tierLabel(s.level)} · ` : ""}{s.target}</span>
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
            <Chip tone={result.outcome === "incident" ? "high" : "off"}>
              {{ muted: "Muted by maintenance", filtered: "Filtered as noise, nobody notified", delayed: "Waits, opens only if it does not clear", incident: "Incident opens" }[result.outcome]}
            </Chip>
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
  const [tab, setTab] = useState<Tab>("filters");
  const [error, setError] = useState("");
  const [ruleEdit, setRuleEdit] = useState<{ rule: Rule | null } | null>(null);
  const [bookEdit, setBookEdit] = useState<{ book: Book | null; copy: boolean } | null>(null);
  const [policyEdit, setPolicyEdit] = useState<{ policy: Policy | null } | null>(null);
  const [maintNew, setMaintNew] = useState(false);
  const load = () => getJson<Automation>("/api/v1/automation").then(setData).catch((e: Error) => setError(e.message));
  const closeRule = (saved: boolean) => (setRuleEdit(null), saved && load());
  const closeBook = (saved: boolean) => (setBookEdit(null), saved && load());
  const closePolicy = (saved: boolean) => (setPolicyEdit(null), saved && load());
  const toggle = (r: Rule) =>
    putJson(`/api/v1/automation/rules/${r.id}`, ruleBody({ ...ruleDraft(r), enabled: !r.enabled })).then(load).catch((e: Error) => setError(e.message));
  useEffect(() => {
    load();
  }, []);
  const p = data?.pipeline;
  const steps: [string, string, string][] = p
    ? [
        ["Collect", "Alertmanager · Zabbix · LibreNMS · syslog", `${p.incidents_today} incidents today`],
        ["Filter noise", "Noise filters, maintenance, parent down", `${p.filtered_today} filtered · ${p.muted_today} quiet in total`],
        ["Route", "Rules pick playbook and escalation", `${p.routed_today} matched a rule`],
        ["Escalate and notify", "Policies, on-call, channels", `${p.notifications_today} messages sent`],
      ]
    : [];
  const pg_rules = usePaged((data?.rules ?? []), "rules");
  const pgBooks = usePaged(data?.playbooks ?? [], "playbooks", "playbooks");
  const pgMaint = usePaged(data?.maintenance ?? [], "maintenance", "windows");
  return (
    <>
      <header className="page-head">
        <div>
          <h1>Automation</h1>
          <span className="muted">What happens between an alarm and someone’s phone</span>
        </div>
        <a className="btn" href="/api/v1/automation/export" title="Rules, playbooks, escalation, noise filters, on-call, SLOs and ForgeAI skills as one JSON file">Export (JSON)</a>
      </header>
      {error ? <div className="banner error" role="alert">{error}</div> : null}
      {p ? (
        <div className="pipeline" role="list" aria-label="Pipeline">
          {steps.map(([title, detail, count], i) => (
            <div key={title} role="listitem" className="card" style={{ gap: 4, padding: 14 }}>
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
      {tab === "forgeai" ? <><ForgeAiSkills /><GoldenSet /><ForgeAiDocs /></> : null}
      {tab === "known" ? <KnownErrorsCard /> : null}
      {data && tab === "filters" ? <NoiseFilters filters={data.filters} canEdit={data.can_edit} onChanged={load} /> : null}
      {data && tab === "rules" ? (
        <>
        {ruleEdit ? <RuleForm key={ruleEdit.rule?.id ?? "new"} rule={ruleEdit.rule} data={data} onDone={closeRule} /> : null}
        <Card title="Routing rules · first match wins" actions={<><span className="small faint">Asset-specific rules are tried before global ones</span>{data.can_edit ? <button className="btn primary" type="button" onClick={() => setRuleEdit({ rule: null })}>New rule</button> : null}</>}>
          <div className="table-wrap">
            <table className="data" style={{ minWidth: 860 }}>
              <thead>
                <tr><th style={{ width: 160 }}>Rule</th><th>When the alarm is</th><th style={{ width: 90 }}>Severity</th><th style={{ width: 150 }}>Playbook</th><th style={{ width: 160 }}>Escalation</th><th style={{ width: 90 }}>Hits 30 d</th><th style={{ width: 70 }}>On</th>{data.can_edit ? <th style={{ width: 230 }}>Change</th> : null}</tr>
              </thead>
              <tbody>
                {pg_rules.page.map((r) => (
                  <tr key={r.id}>
                    <td><div className="cell-stack"><span style={{ fontWeight: 600 }}>{r.name}</span><span className="small muted">{r.description}</span></div></td>
                    <td className="mono">{condition(r.condition)}</td>
                    <td>{r.severity}</td>
                    <td>{r.playbook ?? <span className="faint">—</span>}</td>
                    <td>{r.policy ?? <span className="faint">—</span>}</td>
                    <td className="muted">{r.hits_30d}</td>
                    <td><Chip tone={r.enabled ? "ok" : "off"}>{r.enabled ? "On" : "Off"}</Chip></td>
                    {data.can_edit ? (
                      <td>
                        <div className="row" style={{ gap: 6 }}>
                          <button className="btn" type="button" onClick={() => setRuleEdit({ rule: r })} aria-label={`Edit rule ${r.name}`}>Edit</button>
                          <button className="btn" type="button" onClick={() => toggle(r)} aria-label={`${r.enabled ? "Turn off" : "Turn on"} rule ${r.name}`}>{r.enabled ? "Turn off" : "Turn on"}</button>
                          <RemoveButton url={`/api/v1/automation/rules/${r.id}`} what={r.name} onRemoved={load} />
                        </div>
                      </td>
                    ) : null}
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
          {pg_rules.pager}
        </Card>
        </>
      ) : null}
      {data && tab === "escalation" ? (
        <>
        {policyEdit ? <PolicyForm key={policyEdit.policy?.id ?? "new"} policy={policyEdit.policy} onDone={closePolicy} /> : null}
        {data.can_edit ? <div className="row"><button className="btn primary" type="button" onClick={() => setPolicyEdit({ policy: null })}>New policy</button><a className="btn" href="/app/incidents/on-call">On-call week</a></div> : null}
        <div className="cols">
          {data.policies.map((policy) => (
            <Card key={policy.id} title={policy.name} actions={
              <>
                <span className="small faint">{policy.rules} rules</span>
                {data.can_edit ? (
                  <>
                    <button className="btn" type="button" onClick={() => setPolicyEdit({ policy })} aria-label={`Edit policy ${policy.name}`}>Edit</button>
                    {policy.slug !== "default-warning" ? <RemoveButton url={`/api/v1/automation/policies/${policy.id}`} what={policy.name} onRemoved={load} /> : null}
                  </>
                ) : null}
              </>
            }>
              <Steps steps={policy.steps} />
            </Card>
          ))}
        </div>
        </>
      ) : null}
      {data && tab === "playbooks" ? (
        <>
        {bookEdit ? <PlaybookForm key={`${bookEdit.book?.id ?? "new"}-${bookEdit.copy}`} book={bookEdit.book} copy={bookEdit.copy} onDone={closeBook} /> : null}
        {data.can_edit ? <div className="row"><button className="btn primary" type="button" onClick={() => setBookEdit({ book: null, copy: false })}>New playbook</button></div> : null}
        <div className="cols">
          {pgBooks.page.map((book) => (
            <Card key={book.id} title={book.name} actions={
              <>
                <span className="small faint">{book.rules} rules</span>
                {data.can_edit ? (
                  <>
                    <button className="btn" type="button" onClick={() => setBookEdit({ book, copy: false })} aria-label={`Edit playbook ${book.name}`}>Edit</button>
                    <button className="btn" type="button" onClick={() => setBookEdit({ book, copy: true })} aria-label={`Copy playbook ${book.name}`}>Copy</button>
                    <RemoveButton url={`/api/v1/automation/playbooks/${book.id}`} what={book.name} onRemoved={load} />
                  </>
                ) : null}
              </>
            }>
              {book.description ? <span className="small muted">{book.description}</span> : null}
              <ol style={{ margin: 0, paddingLeft: 20 }}>{book.steps.map((s, i) => <li key={i}>{s}</li>)}</ol>
            </Card>
          ))}
        </div>
        {pgBooks.pager}
        </>
      ) : null}
      {data && tab === "maintenance" ? (
        <>
        {maintNew ? <MaintenanceForm onDone={(saved) => (setMaintNew(false), saved && load())} /> : null}
        <Card title="Maintenance windows" actions={<><span className="small faint">Alarms on these assets are muted while a window is open</span>{data.can_edit_maintenance ? <button className="btn primary" type="button" onClick={() => setMaintNew(true)}>New window</button> : null}</>}>
          {data.maintenance.length === 0 ? <span className="muted">No current or upcoming windows.</span> : null}
          {pgMaint.page.map((w, i) => (
            <div key={i} className="row" style={{ justifyContent: "space-between", borderTop: "1px solid var(--border)", paddingTop: 8 }}>
              <div className="cell-stack"><span style={{ fontWeight: 600 }}>{w.asset}</span><span className="small muted">{w.summary}</span></div>
              <span className="row small">
                {clock(w.starts_at)} – {clock(w.ends_at)}
                <Chip tone={w.active ? "warn" : "off"}>{w.active ? "Active" : "Planned"}</Chip>
                {data.can_edit_maintenance && w.asset_id ? (
                  <button className="btn" type="button" aria-label={`End maintenance on ${w.asset}`}
                    onClick={() => getJson(`/api/v1/maintenance/${encodeURIComponent(w.asset_id!)}`, { method: "DELETE" }).then(load).catch((e: Error) => setError(e.message))}>
                    {w.active ? "End now" : "Cancel"}
                  </button>
                ) : null}
              </span>
            </div>
          ))}
          {pgMaint.pager}
        </Card>
        </>
      ) : null}
      <Simulator />
    </>
  );
}
