import { useState, type FormEvent, type ReactNode } from "react";
import { getJson, postJson, putJson, type Automation } from "../lib/api";
import { Card } from "./ui";

type Rule = Automation["rules"][number];
type Book = Automation["playbooks"][number];

export type RuleDraft = {
  name: string; description: string; enabled: boolean; severity: string; alertname: string;
  metric: string; operator: string; value: string; playbook_id: string; escalation_policy_id: string;
};

export function ruleDraft(r: Rule | null): RuleDraft {
  const c = r?.condition ?? {};
  return {
    name: r?.name ?? "", description: r?.description ?? "", enabled: r?.enabled ?? true, severity: r?.severity ?? "warning",
    alertname: c.alertname ?? "", metric: c.metric ?? "", operator: c.operator ?? ">", value: c.value === undefined ? "" : String(c.value),
    playbook_id: r?.playbook_id ? String(r.playbook_id) : "", escalation_policy_id: r?.policy_id ? String(r.policy_id) : "",
  };
}

/** JSON body for POST / PUT /api/v1/automation/rules. A numeric threshold is sent as a number. */
export function ruleBody(d: RuleDraft) {
  const value = d.value.trim();
  return {
    name: d.name.trim(), description: d.description, enabled: d.enabled, severity: d.severity, alertname: d.alertname.trim(),
    metric: d.metric.trim(), operator: d.operator, value: value === "" ? null : Number.isNaN(Number(value)) ? value : Number(value),
    playbook_id: d.playbook_id ? Number(d.playbook_id) : null, escalation_policy_id: d.escalation_policy_id ? Number(d.escalation_policy_id) : null,
  };
}

function Field({ label, grow = "1 1 180px", children }: { label: string; grow?: string; children: ReactNode }) {
  return <label className="cell-stack small faint" style={{ flex: grow }}>{label}{children}</label>;
}

export function RuleForm({ rule, data, onDone }: { rule: Rule | null; data: Automation; onDone: (saved: boolean) => void }) {
  const [d, setD] = useState<RuleDraft>(() => ruleDraft(rule));
  const [error, setError] = useState("");
  const set = (patch: Partial<RuleDraft>) => setD({ ...d, ...patch });
  const input = (key: keyof RuleDraft, placeholder = "") => (
    <input className="search" style={{ minWidth: 0 }} placeholder={placeholder} value={String(d[key])} onChange={(e) => set({ [key]: e.target.value } as Partial<RuleDraft>)} />
  );
  const save = (e: FormEvent) => {
    e.preventDefault();
    const call = rule ? putJson(`/api/v1/automation/rules/${rule.id}`, ruleBody(d)) : postJson("/api/v1/automation/rules", ruleBody(d));
    call.then(() => onDone(true)).catch((err: Error) => setError(err.message));
  };
  return (
    <Card title={rule ? `Edit rule · ${rule.name}` : "New routing rule"}>
      <form onSubmit={save} className="cell-stack" style={{ gap: 12 }}>
        <div className="row" style={{ alignItems: "flex-end" }}>
          <Field label="Name">{input("name", "disk-full-db")}</Field>
          <Field label="Description" grow="2 1 260px">{input("description", "Oracle archive disk filling up")}</Field>
        </div>
        <div className="row" style={{ alignItems: "flex-end" }}>
          <Field label="Alert name (empty = rule name)">{input("alertname", "NodeFilesystemUsageHigh")}</Field>
          <Field label="Only when metric (optional)">{input("metric", "disk_used_percent")}</Field>
          <Field label="Operator" grow="0 1 100px">
            <select className="search" style={{ minWidth: 0 }} value={d.operator} onChange={(e) => set({ operator: e.target.value })}>
              {[">", ">=", "<", "=="].map((o) => <option key={o} value={o}>{o}</option>)}
            </select>
          </Field>
          <Field label="Threshold" grow="0 1 120px">{input("value", "90")}</Field>
        </div>
        <div className="row" style={{ alignItems: "flex-end" }}>
          <Field label="Severity" grow="0 1 160px">
            <select className="search" style={{ minWidth: 0 }} value={d.severity} onChange={(e) => set({ severity: e.target.value })}>
              {["critical", "high", "warning"].map((s) => <option key={s} value={s}>{s}</option>)}
            </select>
          </Field>
          <Field label="Playbook">
            <select className="search" style={{ minWidth: 0 }} value={d.playbook_id} onChange={(e) => set({ playbook_id: e.target.value })}>
              <option value="">No playbook</option>
              {data.playbooks.map((b) => <option key={b.id} value={b.id}>{b.name}</option>)}
            </select>
          </Field>
          <Field label="Escalation">
            <select className="search" style={{ minWidth: 0 }} value={d.escalation_policy_id} onChange={(e) => set({ escalation_policy_id: e.target.value })}>
              <option value="">Default (default-warning)</option>
              {data.policies.map((p) => <option key={p.id} value={p.id}>{p.name}</option>)}
            </select>
          </Field>
        </div>
        <p className="small faint" style={{ margin: 0 }}>The alert threshold itself lives in Prometheus <span className="mono">alerts.yml</span>; this rule decides what happens when that alert fires.</p>
        <label className="row small"><input type="checkbox" checked={d.enabled} onChange={(e) => set({ enabled: e.target.checked })} /> Enabled</label>
        {error ? <div className="banner error" role="alert">{error}</div> : null}
        <div className="row">
          <button className="btn primary" type="submit" disabled={!d.name.trim()}>Save rule</button>
          <button className="btn" type="button" onClick={() => onDone(false)}>Cancel</button>
        </div>
      </form>
    </Card>
  );
}

export function PlaybookForm({ book, copy, onDone }: { book: Book | null; copy?: boolean; onDone: (saved: boolean) => void }) {
  const [name, setName] = useState(book ? (copy ? `${book.name} (copy)` : book.name) : "");
  const [description, setDescription] = useState(book?.description ?? "");
  const [steps, setSteps] = useState((book?.steps ?? []).join("\n"));
  const [error, setError] = useState("");
  const save = (e: FormEvent) => {
    e.preventDefault();
    const body = { name: name.trim(), description, steps: steps.split("\n").map((s) => s.trim()).filter(Boolean) };
    const call = book && !copy ? putJson(`/api/v1/automation/playbooks/${book.id}`, body) : postJson("/api/v1/automation/playbooks", body);
    call.then(() => onDone(true)).catch((err: Error) => setError(err.message));
  };
  return (
    <Card title={book ? (copy ? `Copy of ${book.name}` : `Edit playbook · ${book.name}`) : "New playbook"}>
      <form onSubmit={save} className="cell-stack" style={{ gap: 12 }}>
        <div className="row" style={{ alignItems: "flex-end" }}>
          <Field label="Name"><input className="search" style={{ minWidth: 0 }} value={name} onChange={(e) => setName(e.target.value)} placeholder="Disk filling up" /></Field>
          <Field label="Description" grow="2 1 260px"><input className="search" style={{ minWidth: 0 }} value={description} onChange={(e) => setDescription(e.target.value)} /></Field>
        </div>
        <Field label="Checklist steps, one per line" grow="1 1 auto">
          <textarea className="search" rows={7} style={{ minWidth: 0, padding: 10 }} value={steps} onChange={(e) => setSteps(e.target.value)} placeholder={"Check disk usage and growth\nFind the owner\nClean up or extend the volume"} />
        </Field>
        {error ? <div className="banner error" role="alert">{error}</div> : null}
        <div className="row">
          <button className="btn primary" type="submit" disabled={!name.trim() || !steps.trim()}>Save playbook</button>
          <button className="btn" type="button" onClick={() => onDone(false)}>Cancel</button>
        </div>
      </form>
    </Card>
  );
}

/** Two-step remove button: the first click asks, the second sends DELETE. */
export function RemoveButton({ url, what, onRemoved }: { url: string; what: string; onRemoved: () => void }) {
  const [asking, setAsking] = useState(false);
  const [error, setError] = useState("");
  if (!asking) return <button className="btn" type="button" onClick={() => setAsking(true)}>Remove</button>;
  return (
    <span className="row" style={{ gap: 6 }}>
      <button className="btn" type="button" style={{ borderColor: "var(--crit)", color: "var(--crit)" }} aria-label={`Confirm remove ${what}`}
        onClick={() => getJson(url, { method: "DELETE" }).then(onRemoved).catch((e: Error) => setError(e.message))}>Remove {what}?</button>
      <button className="btn" type="button" onClick={() => setAsking(false)}>Keep</button>
      {error ? <span className="small" role="alert" style={{ color: "var(--crit)" }}>{error}</span> : null}
    </span>
  );
}

type Policy = Automation["policies"][number];
const LEVELS: { level: number; label: string; minutes: number; target: string }[] = [
  { level: 1, label: "L1 NOC", minutes: 0, target: "team" },
  { level: 2, label: "L2 Shift", minutes: 15, target: "team-lead" },
  { level: 3, label: "L3 Engineer", minutes: 30, target: "engineer" },
  { level: 4, label: "L4 Manager", minutes: 60, target: "manager" },
];

export function PolicyForm({ policy, onDone }: { policy: Policy | null; onDone: (saved: boolean) => void }) {
  const [name, setName] = useState(policy?.name ?? "");
  const [levels, setLevels] = useState(() =>
    LEVELS.map((d) => {
      const saved = policy?.levels?.find((l) => l.level === d.level);
      return { level: d.level, label: d.label, on: saved ? saved.on : !policy && d.level <= 2, minutes: String(saved?.minutes ?? d.minutes), target: saved?.target ?? d.target };
    }),
  );
  const [error, setError] = useState("");
  const setLevel = (i: number, patch: Partial<(typeof levels)[number]>) => setLevels(levels.map((l, j) => (j === i ? { ...l, ...patch } : l)));
  const save = (e: FormEvent) => {
    e.preventDefault();
    const body = { name: name.trim(), levels: levels.map((l) => ({ level: l.level, on: l.on, minutes: Number(l.minutes || 0), target: l.target.trim() })) };
    const call = policy ? putJson(`/api/v1/automation/policies/${policy.id}`, body) : postJson("/api/v1/automation/policies", body);
    call.then(() => onDone(true)).catch((err: Error) => setError(err.message));
  };
  return (
    <Card title={policy ? `Edit escalation · ${policy.name}` : "New escalation policy"}>
      <form onSubmit={save} className="cell-stack" style={{ gap: 12 }}>
        <Field label="Name"><input className="search" style={{ minWidth: 0 }} value={name} onChange={(e) => setName(e.target.value)} placeholder="Database critical" /></Field>
        <p className="small faint" style={{ margin: 0 }}>
          Each level is mailed when the incident is still not acknowledged after its minutes. A recipient is a role (team, team-lead, engineer, manager, oncall) or an email address.
        </p>
        <table className="data">
          <thead><tr><th style={{ width: 70 }}>On</th><th style={{ width: 140 }}>Level</th><th style={{ width: 140 }}>After (min)</th><th>Recipient</th></tr></thead>
          <tbody>
            {levels.map((l, i) => (
              <tr key={l.level}>
                <td><input type="checkbox" aria-label={`${l.label} on`} checked={l.on} onChange={(e) => setLevel(i, { on: e.target.checked })} /></td>
                <td>{l.label}</td>
                <td><input className="search" style={{ minWidth: 0, width: 100 }} type="number" min={0} aria-label={`${l.label} minutes`} value={l.minutes} disabled={!l.on} onChange={(e) => setLevel(i, { minutes: e.target.value })} /></td>
                <td><input className="search" style={{ minWidth: 0, width: "100%" }} list="policy-roles" aria-label={`${l.label} recipient`} value={l.target} disabled={!l.on} onChange={(e) => setLevel(i, { target: e.target.value })} /></td>
              </tr>
            ))}
          </tbody>
        </table>
        <datalist id="policy-roles">{["team", "team-lead", "engineer", "manager", "oncall"].map((r) => <option key={r} value={r} />)}</datalist>
        {error ? <div className="banner error" role="alert">{error}</div> : null}
        <div className="row">
          <button className="btn primary" type="submit" disabled={!name.trim()}>Save policy</button>
          <button className="btn" type="button" onClick={() => onDone(false)}>Cancel</button>
        </div>
      </form>
    </Card>
  );
}

/** datetime-local value for a Date in the browser's time zone. */
export function localInput(d: Date): string {
  const pad = (n: number) => String(n).padStart(2, "0");
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}T${pad(d.getHours())}:${pad(d.getMinutes())}`;
}

export function MaintenanceForm({ onDone }: { onDone: (saved: boolean) => void }) {
  const now = new Date();
  const [assets, setAssets] = useState("");
  const [from, setFrom] = useState(localInput(now));
  const [until, setUntil] = useState(localInput(new Date(now.getTime() + 2 * 3600_000)));
  const [error, setError] = useState("");
  const quick = (hours: number) => (setFrom(localInput(new Date())), setUntil(localInput(new Date(Date.now() + hours * 3600_000))));
  const tonight = () => {
    const start = new Date();
    start.setHours(22, 0, 0, 0);
    const end = new Date(start.getTime() + 8 * 3600_000);
    setFrom(localInput(start));
    setUntil(localInput(end));
  };
  const save = (e: FormEvent) => {
    e.preventDefault();
    postJson("/api/v1/maintenance", { assets: assets.split(/[,\s]+/).filter(Boolean), starts_at: from, ends_at: until })
      .then(() => onDone(true))
      .catch((err: Error) => setError(err.message));
  };
  return (
    <Card title="New maintenance window">
      <form onSubmit={save} className="cell-stack" style={{ gap: 12 }}>
        <Field label="Assets (hostname, id or IP; several separated by commas)" grow="1 1 auto">
          <input className="search" style={{ minWidth: 0 }} value={assets} onChange={(e) => setAssets(e.target.value)} placeholder="db-ora-02, db-ora-03" />
        </Field>
        <div className="row" style={{ alignItems: "flex-end" }}>
          <Field label="From"><input className="search" style={{ minWidth: 0 }} type="datetime-local" value={from} onChange={(e) => setFrom(e.target.value)} /></Field>
          <Field label="Until"><input className="search" style={{ minWidth: 0 }} type="datetime-local" value={until} onChange={(e) => setUntil(e.target.value)} /></Field>
        </div>
        <div className="row" style={{ gap: 6 }}>
          <button className="btn" type="button" onClick={() => quick(1)}>Next hour</button>
          <button className="btn" type="button" onClick={() => quick(4)}>Next 4 hours</button>
          <button className="btn" type="button" onClick={tonight}>Tonight 22:00–06:00</button>
        </div>
        <p className="small faint" style={{ margin: 0 }}>Times are the appliance time zone. While the window is open, alarms on these assets still open incidents, but nobody is mailed or messaged.</p>
        {error ? <div className="banner error" role="alert">{error}</div> : null}
        <div className="row">
          <button className="btn primary" type="submit" disabled={!assets.trim()}>Start maintenance</button>
          <button className="btn" type="button" onClick={() => onDone(false)}>Cancel</button>
        </div>
      </form>
    </Card>
  );
}
