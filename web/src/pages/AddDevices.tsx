import { useEffect, useState, type FormEvent } from "react";
import { Link } from "react-router-dom";
import { MonitoringCheck } from "../components/MonitoringCheck";
import { SnmpFields, snmpPayload, type Snmp } from "../components/SnmpFields";
import { Card, Chip } from "../components/ui";
import { getJson, postJson, type AssetFormOptions } from "../lib/api";
import { clock } from "../lib/format";
import { waitForJob } from "../lib/jobs";

type Found = {
  ip: string; alive: boolean; open_ports: number[]; exporter: string; detect_message: string;
  snmp: { sys_descr?: string; sys_name?: string; sys_object_id?: string; sys_location?: string };
  snmp_note: string; watch: string;
  suggested: { hostname: string; type: string; monitoring_profile: string; scrape_address: string; vendor: string; location: string };
};
type Candidate = { id: number; ip: string; hostname: string; proposed_role: string; open_ports: number[]; snmp_ok: boolean; status: string; asset_id: string; seen_at: string | null };
type Status = { cidrs: string[]; suggested: string[]; mode: string; scan: { id: number; status: string } | null; counts: Record<string, number> };

const PORT_NAME: Record<number, string> = { 22: "SSH", 80: "HTTP", 443: "HTTPS", 161: "SNMP", 9100: "node_exporter", 9182: "windows_exporter", 10050: "Zabbix agent", 12345: "Alloy" };
const SNMP_TYPES = /network|switch|router|firewall|storage|nas|printer/i;

/** One device: identify what answers, review, add, then watch the monitoring check turn green. */
function OneDevice({ options }: { options: AssetFormOptions | null }) {
  const [ip, setIp] = useState("");
  const [snmp, setSnmp] = useState<Snmp>({ snmp_version: "v2c", snmp_community_mode: "default" });
  const [port, setPort] = useState("161");
  const [found, setFound] = useState<Found | null>(null);
  const [form, setForm] = useState({ hostname: "", type: "", environment: "Production", owner: "", contact_name: "", owner_email: "", owner_phone: "" });
  const [busy, setBusy] = useState("");
  const [error, setError] = useState("");
  const [added, setAdded] = useState("");
  const identify = async (e: FormEvent) => {
    e.preventDefault();
    setError("");
    setFound(null);
    setBusy("Looking at the address…");
    try {
      const { job } = await postJson<{ job: number }>("/api/v1/onboard/identify", { ip, snmp_port: Number(port) || 161, snmp: snmpPayload(snmp) });
      const done = await waitForJob<Found>(job, { every: 1000 });
      if (done.status === "error" || !done.result) throw new Error(done.error || "Could not look at the address.");
      setFound(done.result);
      setForm((f) => ({ ...f, hostname: done.result!.suggested.hostname || ip.replace(/[.:]/g, "-"), type: done.result!.suggested.type }));
    } catch (err) {
      setError((err as Error).message);
    } finally {
      setBusy("");
    }
  };
  const snmpProblem = (): string => {
    const v = snmpPayload(snmp);
    if (v.snmp_version !== "v3") return v.snmp_community_mode === "custom" && !v.snmp_community ? "Enter the community string." : "";
    if (!v.snmp_v3_user) return "Enter the SNMPv3 user.";
    if (v.snmp_v3_level !== "noAuthNoPriv" && (v.snmp_v3_auth_pass ?? "").length < 8) return "The auth password needs at least 8 characters.";
    if (v.snmp_v3_level === "authPriv" && (v.snmp_v3_priv_pass ?? "").length < 8) return "The privacy password needs at least 8 characters.";
    return "";
  };
  const add = async () => {
    if (!found) return;
    const usesSnmp = SNMP_TYPES.test(form.type);
    const problem = usesSnmp ? snmpProblem() : "";
    if (problem) return setError(problem);
    setBusy("Adding…");
    setError("");
    try {
      const body = {
        ...form, owner: form.owner || undefined, ip: found.ip, asset_id: "", monitoring_profile: found.suggested.monitoring_profile, scrape_address: found.suggested.scrape_address,
        snmp_port: usesSnmp && Number(port) !== 161 ? Number(port) : undefined,
        extras: { ...(usesSnmp ? snmpPayload(snmp) : {}), ...(found.suggested.location ? { site: found.suggested.location } : {}) },
      };
      const saved = await postJson<{ asset_id: string }>("/api/v1/assets", body);
      setAdded(saved.asset_id);
    } catch (err) {
      setError((err as Error).message);
    } finally {
      setBusy("");
    }
  };
  if (added) {
    return (
      <Card title={`Added ${form.hostname}`}>
        <span className="small muted">
          ForgeSRE now tells Prometheus about it (it asks for targets every 30 seconds){SNMP_TYPES.test(form.type) ? " and writes its SNMP credentials into snmp_exporter" : ""}.
          The check below runs in the background; give it a minute and press Check again until every step is green.
        </span>
        <MonitoringCheck assetId={added} auto />
        <div className="row">
          <Link className="btn" to={`/inventory?q=${encodeURIComponent(form.hostname)}`}>Open in inventory</Link>
          <button className="btn" type="button" onClick={() => (setAdded(""), setFound(null), setIp(""))}>Add another device</button>
        </div>
      </Card>
    );
  }
  const types = options?.types.flatMap((g) => g.names).filter((n) => !n.startsWith("Auto")) ?? [];
  return (
    <>
      <Card title="1 · Where is it">
        <form className="cell-stack" style={{ gap: 12 }} onSubmit={identify}>
          <div className="row" style={{ gap: 10, alignItems: "flex-end" }}>
            <label className="cell-stack small faint" style={{ flex: "1 1 220px" }}>
              IP address
              <input className="search" style={{ minWidth: 0 }} required value={ip} onChange={(e) => setIp(e.target.value)} placeholder="10.10.20.13" />
            </label>
            <label className="cell-stack small faint" style={{ width: 110 }}>
              SNMP port
              <input className="search" style={{ minWidth: 0 }} type="number" value={port} onChange={(e) => setPort(e.target.value)} />
            </label>
            <button className="btn primary" type="submit" disabled={Boolean(busy)}>{busy || "Identify"}</button>
          </div>
          <SnmpFields value={snmp} onChange={setSnmp} />
          <span className="small faint">
            Servers need no SNMP: ForgeSRE looks for node_exporter (Linux, port 9100) and windows_exporter (Windows, port 9182). For switches,
            routers, firewalls and storage it asks SNMP for the device name and model with the credentials above.
          </span>
        </form>
      </Card>
      {error ? <div className="banner error" role="alert">{error}</div> : null}
      {found ? (
        <Card title="2 · What answered">
          {!found.alive ? <div className="banner" role="note">Nothing answered at {found.ip}. You can still add it, but nothing will be monitored until it answers.</div> : null}
          <div className="row" style={{ gap: 6, flexWrap: "wrap" }}>
            {found.open_ports.length ? found.open_ports.map((p) => <Chip key={p} tone="ok">{PORT_NAME[p] ?? p} · {p}</Chip>) : <Chip tone="off">No open ports</Chip>}
            {found.exporter ? <Chip tone="info">exporter: {found.exporter}</Chip> : null}
          </div>
          {found.snmp.sys_descr ? (
            <div className="cell-stack small" style={{ gap: 2 }}>
              <span><span className="faint">SNMP name</span> {found.snmp.sys_name}</span>
              <span><span className="faint">Model</span> {found.snmp.sys_descr}</span>
              {found.snmp.sys_location ? <span><span className="faint">Location</span> {found.snmp.sys_location}</span> : null}
            </div>
          ) : null}
          {found.snmp_note ? <span className="small muted">{found.snmp_note}</span> : null}
          {found.detect_message ? <span className="small faint">{found.detect_message}</span> : null}
          <span className="small"><strong>How ForgeSRE will watch it:</strong> {found.watch}</span>
        </Card>
      ) : null}
      {found ? (
        <Card title="3 · Check and add">
          <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fill, minmax(220px, 1fr))", gap: 10 }}>
            <label className="cell-stack small faint">Hostname<input className="search" style={{ minWidth: 0 }} required value={form.hostname} onChange={(e) => setForm({ ...form, hostname: e.target.value })} /></label>
            <label className="cell-stack small faint">
              Type
              <select className="search" style={{ minWidth: 0 }} value={form.type} onChange={(e) => setForm({ ...form, type: e.target.value })}>
                {[form.type, ...types.filter((t) => t !== form.type)].filter(Boolean).map((t) => <option key={t} value={t}>{t}</option>)}
              </select>
            </label>
            <label className="cell-stack small faint">
              Environment
              <select className="search" style={{ minWidth: 0 }} value={form.environment} onChange={(e) => setForm({ ...form, environment: e.target.value })}>
                {(options?.environments ?? ["Production"]).map((t) => <option key={t} value={t}>{t}</option>)}
              </select>
            </label>
            <label className="cell-stack small faint">Owner (team)<input className="search" style={{ minWidth: 0 }} list="add-owners" value={form.owner} onChange={(e) => setForm({ ...form, owner: e.target.value })} /></label>
            <label className="cell-stack small faint">Contact<input className="search" style={{ minWidth: 0 }} value={form.contact_name} onChange={(e) => setForm({ ...form, contact_name: e.target.value })} /></label>
            <label className="cell-stack small faint">Contact email<input className="search" style={{ minWidth: 0 }} type="email" value={form.owner_email} onChange={(e) => setForm({ ...form, owner_email: e.target.value })} /></label>
            <label className="cell-stack small faint">Contact phone<input className="search" style={{ minWidth: 0 }} value={form.owner_phone} onChange={(e) => setForm({ ...form, owner_phone: e.target.value })} /></label>
          </div>
          <datalist id="add-owners">{(options?.owners ?? []).map((o) => <option key={o} value={o} />)}</datalist>
          <span className="small faint">The contact gets the alarm mail at Tier 1 when no on-call person is set, so fill it in.</span>
          <div className="row"><button className="btn primary" type="button" onClick={add} disabled={Boolean(busy) || !form.hostname.trim()}>{busy || "Add and start monitoring"}</button></div>
        </Card>
      ) : null}
    </>
  );
}

/** Scan ranges in the background, then add or ignore what was found. */
function ScanNetwork() {
  const [status, setStatus] = useState<Status | null>(null);
  const [rows, setRows] = useState<Candidate[]>([]);
  const [cidrs, setCidrs] = useState("");
  const [error, setError] = useState("");
  const [checking, setChecking] = useState("");
  const load = () => {
    getJson<Status>("/api/v1/discovery/status").then((s) => (setStatus(s), setCidrs((c) => c || (s.cidrs.length ? s.cidrs : s.suggested).join(", ")))).catch((e: Error) => setError(e.message));
    getJson<Candidate[]>("/api/v1/discovery/candidates").then(setRows).catch((e: Error) => setError(e.message));
  };
  useEffect(load, []);
  useEffect(() => {
    if (!status?.scan) return;
    const t = setTimeout(load, 4000);
    return () => clearTimeout(t);
  }, [status]);
  const scan = (e: FormEvent) => {
    e.preventDefault();
    setError("");
    postJson("/api/v1/discovery/scan", cidrs.split(/[,\s]+/).filter(Boolean)).then(load).catch((err: Error) => setError(err.message));
  };
  const act = (id: number, what: "approve" | "ignore") =>
    postJson<{ asset_id?: string }>(`/api/v1/discovery/candidates/${id}/${what}`, {})
      .then((r) => (what === "approve" && r.asset_id ? setChecking(r.asset_id) : null, load()))
      .catch((err: Error) => setError(err.message));
  const fresh = rows.filter((r) => r.status === "new");
  return (
    <>
      <Card title="Scan a network">
        <form className="row" style={{ gap: 10, alignItems: "flex-end" }} onSubmit={scan}>
          <label className="cell-stack small faint" style={{ flex: "1 1 320px" }}>
            Ranges (CIDR, comma separated, up to 256 addresses each)
            <input className="search" style={{ minWidth: 0 }} value={cidrs} onChange={(e) => setCidrs(e.target.value)} placeholder="10.10.20.0/24" />
          </label>
          <button className="btn primary" type="submit" disabled={Boolean(status?.scan)}>{status?.scan ? `Scanning (${status.scan.status})…` : "Scan now"}</button>
        </form>
        <span className="small faint">
          Each address is asked on TCP 22, 80, 443, 9100, 9182, 10050, 12345 and SNMP UDP/161 with the default community. What answers is listed
          below as found; nothing becomes an asset until you add it. Mode: {status?.mode ?? "…"}.
        </span>
      </Card>
      {error ? <div className="banner error" role="alert">{error}</div> : null}
      {checking ? (
        <Card title={`Added ${checking}`}><MonitoringCheck assetId={checking} auto /></Card>
      ) : null}
      <Card title={`Found · ${fresh.length} new`}>
        {rows.length === 0 ? <span className="muted">Nothing found yet. Scan a range above.</span> : null}
        {rows.length ? (
          <div className="table-wrap"><table className="data" style={{ minWidth: 760 }}>
            <thead><tr><th>Address</th><th>Name</th><th>Looks like</th><th>Answers on</th><th>Seen</th><th style={{ width: 170 }}></th></tr></thead>
            <tbody>
              {rows.map((r) => (
                <tr key={r.id}>
                  <td className="mono">{r.ip}</td>
                  <td>{r.hostname || <span className="faint">—</span>}</td>
                  <td className="small">{r.proposed_role}</td>
                  <td className="small muted">{r.open_ports.map((p) => PORT_NAME[p] ?? p).join(", ") || "—"}</td>
                  <td className="small faint">{clock(r.seen_at)}</td>
                  <td>
                    {r.status === "new" ? (
                      <div className="row" style={{ gap: 6 }}>
                        <button className="btn primary" type="button" onClick={() => act(r.id, "approve")}>Add</button>
                        <button className="btn" type="button" onClick={() => act(r.id, "ignore")}>Ignore</button>
                      </div>
                    ) : r.asset_id ? <Link to={`/inventory?q=${encodeURIComponent(r.asset_id)}`}>{r.asset_id}</Link> : <Chip tone="off">{r.status}</Chip>}
                  </td>
                </tr>
              ))}
            </tbody>
          </table></div>
        ) : null}
      </Card>
    </>
  );
}

const TABS = [["one", "One device"], ["scan", "Scan a network"], ["import", "From another system"]] as const;

/** Inventory › Add devices. */
export function AddDevicesPage() {
  const [tab, setTab] = useState<(typeof TABS)[number][0]>("one");
  const [options, setOptions] = useState<AssetFormOptions | null>(null);
  useEffect(() => void getJson<AssetFormOptions>("/api/v1/inventory-form").then(setOptions).catch(() => setOptions(null)), []);
  return (
    <>
      <header className="page-head">
        <div>
          <span className="small faint"><Link to="/inventory">Inventory</Link> / Add devices</span>
          <h1>Add devices</h1>
          <span className="muted">Add a server or network device, and see for yourself that metrics and alarms really arrive.</span>
        </div>
      </header>
      <div className="row" role="tablist">
        {TABS.map(([k, l]) => <button key={k} role="tab" aria-selected={tab === k} className="btn" aria-pressed={tab === k} onClick={() => setTab(k)}>{l}</button>)}
      </div>
      {tab === "one" ? <OneDevice options={options} /> : null}
      {tab === "scan" ? <ScanNetwork /> : null}
      {tab === "import" ? (
        <Card title="From another system">
          <span className="small muted">
            NetBox, Zabbix, GLPI and LibreNMS are read in the background (every 6 hours when turned on). To read one now, an Admin presses
            <strong> Sync now</strong> on its card in <Link to="/settings">Settings › Integrations</Link>. Imported assets then show up in the
            inventory, where <strong>Check monitoring</strong> proves they are watched. A CSV import is still on the <a href="/assets">classic page</a>.
          </span>
        </Card>
      ) : null}
    </>
  );
}
