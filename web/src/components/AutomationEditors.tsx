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
