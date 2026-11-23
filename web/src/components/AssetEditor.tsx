import { useState, type FormEvent } from "react";
import { postJson, type AssetCard, type AssetFormOptions } from "../lib/api";
import { SnmpFields, snmpPayload, type Snmp } from "./SnmpFields";
import { Card } from "./ui";

export type EditorMode = "new" | "edit" | "clone";

type Draft = Record<string, string>;
const BASE = ["hostname", "asset_id", "ip", "type", "environment", "owner", "contact_name", "owner_email", "owner_phone", "scrape_address", "snmp_port", "notes"] as const;
const SNMP_TYPES = /network|switch|router|firewall|storage|qnap|printer|nas|\bups\b|\bpdu\b|environment sensor/i;

function toDraft(mode: EditorMode, card: AssetCard | null): Draft {
  const d: Draft = Object.fromEntries(BASE.map((k) => [k, ""]));
  d.type = "Auto (detect exporter)";
  d.environment = "Production";
  if (!card) return d;
  for (const key of BASE) {
    const value = (card as unknown as Record<string, unknown>)[key];
    if (typeof value === "string" || typeof value === "number") d[key] = String(value || "");
  }
  for (const [key, value] of Object.entries(card.extras ?? {})) if (typeof value === "string") d[`x:${key}`] = value;
  if (mode === "clone") {
    d.hostname = "";
    d.asset_id = "";
    d.ip = "";
    d.scrape_address = "";
  }
  return d;
}

/** Request body for create / update / clone. Saved extras the form does not show (support, maintenance) are sent back unchanged. */
export function toBody(d: Draft, card: AssetCard | null, mode: EditorMode) {
  const extras: Record<string, string> = {};
  for (const [key, value] of Object.entries(card?.extras ?? {})) if (typeof value === "string" && !key.endsWith("_set")) extras[key] = value;
  for (const [key, value] of Object.entries(d)) if (key.startsWith("x:")) extras[key.slice(2)] = value.trim();
  const body: Record<string, unknown> = { extras };
  for (const key of BASE) {
    if (key === "asset_id" && mode === "edit") continue;
    if (key === "snmp_port") {
      if (d.snmp_port.trim()) body.snmp_port = Number(d.snmp_port);
      continue;
    }
    body[key] = d[key].trim();
  }
  if (mode !== "edit" && !d.scrape_address.trim()) delete body.scrape_address;
  return body;
}

export function AssetEditor({ mode, card, options, onDone, onDeleted }: {
  mode: EditorMode;
  card: AssetCard | null;
  options: AssetFormOptions;
  onDone: (assetId: string | null) => void;
  onDeleted?: () => void;
}) {
  const [d, setD] = useState<Draft>(() => toDraft(mode, card));
  const [snmpV, setSnmpV] = useState<Snmp>(() => {
    const saved = Object.entries(card?.extras ?? {}).filter(([k, v]) => k.startsWith("snmp_") && typeof v === "string") as [string, string][];
    return saved.length ? Object.fromEntries(saved) : { snmp_version: "v2c", snmp_community_mode: "default" };
  });
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const [confirmDelete, setConfirmDelete] = useState(false);
  const set = (key: string, value: string) => setD((prev) => ({ ...prev, [key]: value }));
  const labels = Object.fromEntries(options.extra_fields.map((f) => [f.key, f]));

  const save = (e: FormEvent) => {
    e.preventDefault();
    setBusy(true);
    setError("");
    const id = card ? encodeURIComponent(card.asset_id) : "";
    const url = mode === "new" ? "/api/v1/assets" : mode === "edit" ? `/api/v1/assets/${id}` : `/api/v1/assets/${id}/clone`;
    const body = toBody(d, card, mode);
    if (SNMP_TYPES.test(d.type)) body.extras = { ...(body.extras as Record<string, string>), ...snmpPayload(snmpV) };
    postJson<{ asset_id: string }>(url, body)
      .then((saved) => onDone(saved.asset_id))
      .catch((err: Error) => setError(err.message))
      .finally(() => setBusy(false));
  };
  const remove = () => {
    if (!card) return;
    setBusy(true);
    postJson(`/api/v1/assets/${encodeURIComponent(card.asset_id)}/delete`, {})
      .then(() => onDeleted?.())
      .catch((err: Error) => setError(err.message))
      .finally(() => setBusy(false));
  };

  const field = (key: string, label: string, opts: { placeholder?: string; type?: string; list?: string; required?: boolean; max?: number } = {}) => (
    <label className="cell-stack small faint" style={{ flex: "1 1 200px" }}>
      {label}
      <input
        className="search"
        style={{ minWidth: 0 }}
        type={opts.type ?? "text"}
        list={opts.list}
        required={opts.required}
        maxLength={opts.max}
        placeholder={opts.placeholder}
        value={d[key] ?? ""}
        onChange={(e) => set(key, e.target.value)}
      />
    </label>
  );
  const extra = (key: string, placeholder = "", list?: string) =>
    labels[key] ? field(`x:${key}`, labels[key].label, { placeholder, list, max: labels[key].max }) : null;
  const heading = (text: string) => <h3 className="small faint" style={{ margin: "4px 0 0", textTransform: "uppercase", letterSpacing: ".05em" }}>{text}</h3>;
  const title = mode === "new" ? "Add asset" : mode === "edit" ? `Edit · ${card?.hostname}` : `Clone · ${card?.hostname}`;
  const snmp = SNMP_TYPES.test(d.type);

  return (
    <Card title={title}>
      <form onSubmit={save} className="cell-stack" style={{ gap: 12 }}>
        {heading("Identity")}
        <div className="row" style={{ alignItems: "flex-end" }}>
          {field("hostname", "Hostname", { placeholder: "esx-prod-03", required: true })}
          {field("ip", "IP address", { placeholder: "10.10.20.13" })}
        </div>
        <div className="row" style={{ alignItems: "flex-end" }}>
          <label className="cell-stack small faint" style={{ flex: "1 1 200px" }}>
            Type
            <select className="search" style={{ minWidth: 0 }} value={d.type} onChange={(e) => set("type", e.target.value)}>
              {options.types.map((g) => (
                <optgroup key={g.group} label={g.group}>
                  {g.names.map((n) => <option key={n} value={n}>{n}</option>)}
                </optgroup>
              ))}
              {d.type && !options.types.some((g) => g.names.includes(d.type)) ? <option value={d.type}>{d.type}</option> : null}
            </select>
          </label>
          {field("environment", "Environment", { list: "asset-environments" })}
          {mode !== "edit" ? field("asset_id", "Asset id (optional)", { placeholder: "made from the hostname" }) : null}
        </div>
        {heading("Location")}
        <div className="row" style={{ alignItems: "flex-end" }}>
          {extra("site", "DC Beograd", "asset-sites")}
          {extra("row", "A")}
          {extra("rack", "A07")}
          {extra("rack_u", "12")}
          {extra("u_height", "1")}
        </div>
        <div className="row" style={{ alignItems: "flex-end" }}>
          {extra("customer", "ERP prod")}
          {extra("vlan", "120")}
        </div>
        {heading("Owner and contacts")}
        <div className="row" style={{ alignItems: "flex-end" }}>
          {field("owner", "Owner (team)", { list: "asset-owners", placeholder: "Compute" })}
          {field("contact_name", "Contact name")}
        </div>
        <div className="row" style={{ alignItems: "flex-end" }}>
          {field("owner_email", "Contact email", { type: "email" })}
          {field("owner_phone", "Contact phone", { type: "tel" })}
        </div>
        <div className="row" style={{ alignItems: "flex-end" }}>
          {extra("backup_name")}
          {extra("backup_email")}
          {extra("backup_phone")}
        </div>
        {heading("Monitoring")}
        <div className="row" style={{ alignItems: "flex-end" }}>
          {field("scrape_address", "Scrape address", { placeholder: "empty = default port for the type" })}
          {snmp ? field("snmp_port", "SNMP port", { type: "number", placeholder: "161" }) : null}
        </div>
        {snmp ? <SnmpFields value={snmpV} onChange={setSnmpV} saved={mode === "edit" ? card?.extras ?? {} : {}} /> : null}
        {heading("Support")}
        <div className="row" style={{ alignItems: "flex-end" }}>
          {extra("support_hours", "Mon–Fri 08–16")}
          {extra("timezone", "Europe/Belgrade")}
          {extra("contract", "ProSupport to 2027")}
        </div>
        <div className="row" style={{ alignItems: "flex-end" }}>
          {field("x:support_to", "Support / warranty until", { type: "date" })}
          {field("x:eol_date", labels.eol_date?.label ?? "End of life", { type: "date" })}
          {field("x:license_to", labels.license_to?.label ?? "Licence expires", { type: "date" })}
        </div>
        <label className="cell-stack small faint">
          {labels.runbook_note?.label ?? "Runbook note"}
          <textarea className="search" rows={3} style={{ minWidth: 0, padding: 10 }} value={d["x:runbook_note"] ?? ""} onChange={(e) => set("x:runbook_note", e.target.value)} />
        </label>
        <label className="cell-stack small faint">
          Notes
          <textarea className="search" rows={2} style={{ minWidth: 0, padding: 10 }} value={d.notes} onChange={(e) => set("notes", e.target.value)} />
        </label>
        <datalist id="asset-environments">{options.environments.map((v) => <option key={v} value={v} />)}</datalist>
        <datalist id="asset-owners">{options.owners.map((v) => <option key={v} value={v} />)}</datalist>
        <datalist id="asset-sites">{options.sites.map((v) => <option key={v} value={v} />)}</datalist>
        {error ? <div className="banner error" role="alert">{error}</div> : null}
        <div className="row" style={{ justifyContent: "space-between" }}>
          <div className="row">
            <button className="btn primary" type="submit" disabled={busy || !d.hostname.trim()}>{mode === "edit" ? "Save changes" : mode === "clone" ? "Create copy" : "Add asset"}</button>
            <button className="btn" type="button" onClick={() => onDone(null)}>Cancel</button>
          </div>
          {mode === "edit" && onDeleted ? (
            confirmDelete ? (
              <div className="row">
                <span className="small" style={{ color: "var(--crit)" }}>Delete {card?.hostname} and stop monitoring it?</span>
                <button className="btn" type="button" style={{ borderColor: "var(--crit)", color: "var(--crit)" }} disabled={busy} onClick={remove}>Yes, delete</button>
                <button className="btn" type="button" onClick={() => setConfirmDelete(false)}>Keep</button>
              </div>
            ) : (
              <button className="btn" type="button" onClick={() => setConfirmDelete(true)}>Delete asset</button>
            )
          ) : null}
        </div>
      </form>
    </Card>
  );
}
