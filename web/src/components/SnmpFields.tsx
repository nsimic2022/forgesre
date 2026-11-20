/** SNMP credentials for one device: v1/v2c with the default or a custom community, or a v3 user.
 *  Values use the asset extras keys (snmp_version, snmp_community, snmp_v3_*). Secrets are
 *  write-only: an empty box keeps what is saved, and saved ones only show as "saved". */
export type Snmp = Record<string, string>;

const LEVELS: [string, string][] = [["noAuthNoPriv", "No auth, no privacy"], ["authNoPriv", "Auth, no privacy"], ["authPriv", "Auth and privacy (encrypted)"]];
const AUTH = ["SHA", "SHA256", "SHA384", "SHA512", "SHA224", "MD5"];
const PRIV = ["AES", "AES256", "AES192", "AES256C", "AES192C", "DES"];

export function SnmpFields({ value, onChange, saved = {} }: { value: Snmp; onChange: (v: Snmp) => void; saved?: Record<string, unknown> }) {
  const set = (patch: Snmp) => onChange({ ...value, ...patch });
  const version = value.snmp_version || "v2c";
  const sel = (label: string, key: string, options: [string, string][], fallback: string) => (
    <label className="cell-stack small faint">
      {label}
      <select className="search" style={{ minWidth: 0 }} value={value[key] || fallback} onChange={(e) => set({ [key]: e.target.value })}>
        {options.map(([v, l]) => <option key={v} value={v}>{l}</option>)}
      </select>
    </label>
  );
  const secret = (label: string, key: string) => (
    <label className="cell-stack small faint">
      {label}{saved[`${key}_set`] ? " (saved; empty keeps it)" : ""}
      <input className="search" style={{ minWidth: 0 }} type="password" autoComplete="new-password" value={value[key] || ""} onChange={(e) => set({ [key]: e.target.value })} />
    </label>
  );
  return (
    <fieldset className="cell-stack" style={{ gap: 10, border: "1px solid var(--border)", borderRadius: 10, padding: 12, margin: 0 }}>
      <legend className="small faint" style={{ padding: "0 6px" }}>SNMP</legend>
      {sel("Version", "snmp_version", [["v2c", "v2c"], ["v1", "v1"], ["v3", "v3 (user and password)"]], "v2c")}
      {version !== "v3" ? (
        <>
          {sel("Community", "snmp_community_mode", [["default", "The appliance default (SNMP_COMMUNITY)"], ["custom", "Its own community"]], "default")}
          {value.snmp_community_mode === "custom" ? secret("Community string", "snmp_community") : null}
        </>
      ) : (
        <>
          <label className="cell-stack small faint">
            User
            <input className="search" style={{ minWidth: 0 }} value={value.snmp_v3_user || ""} onChange={(e) => set({ snmp_v3_user: e.target.value })} />
          </label>
          {sel("Security level", "snmp_v3_level", LEVELS, "authPriv")}
          {(value.snmp_v3_level || "authPriv") !== "noAuthNoPriv" ? (
            <div className="row" style={{ gap: 10, alignItems: "flex-end" }}>
              {sel("Auth protocol", "snmp_v3_auth_proto", AUTH.map((a) => [a, a]), "SHA")}
              {secret("Auth password (8+)", "snmp_v3_auth_pass")}
            </div>
          ) : null}
          {(value.snmp_v3_level || "authPriv") === "authPriv" ? (
            <div className="row" style={{ gap: 10, alignItems: "flex-end" }}>
              {sel("Privacy protocol", "snmp_v3_priv_proto", PRIV.map((a) => [a, a]), "AES")}
              {secret("Privacy password (8+)", "snmp_v3_priv_pass")}
            </div>
          ) : null}
        </>
      )}
      <span className="small faint">Stored on the asset, never shown again. ForgeSRE writes them into snmp_exporter for you; no shell step.</span>
    </fieldset>
  );
}

/** Only the keys the chosen version uses, with defaults filled in, for the asset API. */
export function snmpPayload(v: Snmp): Snmp {
  const version = v.snmp_version || "v2c";
  if (version !== "v3") {
    const custom = v.snmp_community_mode === "custom";
    return { snmp_version: version, snmp_community_mode: custom ? "custom" : "default", ...(custom && v.snmp_community ? { snmp_community: v.snmp_community } : {}) };
  }
  const level = v.snmp_v3_level || "authPriv";
  const out: Snmp = { snmp_version: "v3", snmp_v3_user: v.snmp_v3_user || "", snmp_v3_level: level };
  if (level !== "noAuthNoPriv") Object.assign(out, { snmp_v3_auth_proto: v.snmp_v3_auth_proto || "SHA", ...(v.snmp_v3_auth_pass ? { snmp_v3_auth_pass: v.snmp_v3_auth_pass } : {}) });
  if (level === "authPriv") Object.assign(out, { snmp_v3_priv_proto: v.snmp_v3_priv_proto || "AES", ...(v.snmp_v3_priv_pass ? { snmp_v3_priv_pass: v.snmp_v3_priv_pass } : {}) });
  return out;
}
