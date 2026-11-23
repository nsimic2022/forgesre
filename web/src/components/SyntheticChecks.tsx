import { useEffect, useState, type FormEvent } from "react";
import { getJson, postJson } from "../lib/api";
import { Card, Chip } from "./ui";

type Check = { id: number; name: string; kind: string; target: string; asset: string; severity: string; enabled: boolean; up: number | null; seconds: number | null };
type Data = { checks: Check[]; error: string; can_edit: boolean };

const HINT: Record<string, string> = { http: "https://erp.dc.local/health", tcp: "db01.dc.local:5432", dns: "10.0.0.53", icmp: "10.0.0.1" };
const EMPTY = { name: "", kind: "http", target: "", asset: "", severity: "critical" };

/** Settings › Checks: HTTP, TCP, DNS and ping probes run from the appliance (blackbox_exporter). */
export function SyntheticChecks() {
  const [data, setData] = useState<Data | null>(null);
  const [form, setForm] = useState(EMPTY);
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const load = () => getJson<Data>("/api/v1/synthetic").then(setData).catch((e: Error) => setError(e.message));
  useEffect(() => void load(), []);
  const add = async (e: FormEvent) => {
    e.preventDefault();
    setBusy(true);
    setError("");
    try {
      await postJson("/api/v1/synthetic", form);
      setForm(EMPTY);
      await load();
    } catch (err) {
      setError((err as Error).message);
    } finally {
      setBusy(false);
    }
  };
  const remove = async (id: number) => {
    await getJson(`/api/v1/synthetic/${id}`, { method: "DELETE" }).catch((e: Error) => setError(e.message));
    await load();
  };
  const checks = Array.isArray(data?.checks) ? data!.checks : [];
  const set = (k: keyof typeof EMPTY, v: string) => setForm((f) => ({ ...f, [k]: v }));
  return (
    <Card title="Synthetic checks" actions={<span className="small faint">probed every 60 s</span>}>
      {error ? <div className="banner error" role="alert">{error}</div> : null}
      {data?.error ? <div className="banner">Prometheus: {data.error}</div> : null}
      {checks.length === 0 ? <span className="muted">No synthetic check yet. Add a URL, a port, a DNS server or a host to ping.</span> : (
        <div className="table-wrap">
          <table className="data">
            <thead><tr><th style={{ width: 90 }}>Result</th><th>Check</th><th style={{ width: 60 }}>Kind</th><th>Target</th><th style={{ width: 80 }}>Time</th><th style={{ width: 90 }}>Severity</th>{data?.can_edit ? <th style={{ width: 80 }} /> : null}</tr></thead>
            <tbody>
              {checks.map((c) => (
                <tr key={c.id}>
                  <td>{!c.enabled ? <Chip tone="off">off</Chip> : c.up === null ? <Chip tone="off">waiting</Chip> : c.up >= 1 ? <Chip tone="ok">ok</Chip> : <Chip tone="crit">failing</Chip>}</td>
                  <td>{c.name}{c.asset && c.asset !== c.name ? <span className="small faint"> · {c.asset}</span> : null}</td>
                  <td className="small">{c.kind}</td>
                  <td className="mono small">{c.target}</td>
                  <td className="small">{c.seconds === null ? "–" : `${Math.round(c.seconds * 1000)} ms`}</td>
                  <td className="small">{c.severity}</td>
                  {data?.can_edit ? <td><button className="btn" type="button" aria-label={`Remove ${c.name}`} onClick={() => void remove(c.id)}>Remove</button></td> : null}
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
      {data?.can_edit ? (
        <form className="row" style={{ gap: 8, alignItems: "flex-end", flexWrap: "wrap" }} onSubmit={add} aria-label="Add synthetic check">
          <label className="cell-stack small faint">Name<input className="search" value={form.name} onChange={(e) => set("name", e.target.value)} placeholder="erp-web" required /></label>
          <label className="cell-stack small faint">Kind
            <select className="search" value={form.kind} onChange={(e) => set("kind", e.target.value)}>
              <option value="http">HTTP(S)</option><option value="tcp">TCP port</option><option value="dns">DNS server</option><option value="icmp">Ping</option>
            </select>
          </label>
          <label className="cell-stack small faint" style={{ flex: "1 1 260px" }}>Target<input className="search" value={form.target} onChange={(e) => set("target", e.target.value)} placeholder={HINT[form.kind]} required /></label>
          <label className="cell-stack small faint">Asset (optional)<input className="search" value={form.asset} onChange={(e) => set("asset", e.target.value)} placeholder="erp-app-01" /></label>
          <label className="cell-stack small faint">Severity
            <select className="search" value={form.severity} onChange={(e) => set("severity", e.target.value)}>
              <option value="critical">critical (3 min)</option><option value="warning">warning (10 min)</option>
            </select>
          </label>
          <button className="btn primary" type="submit" disabled={busy}>Add check</button>
        </form>
      ) : null}
    </Card>
  );
}
