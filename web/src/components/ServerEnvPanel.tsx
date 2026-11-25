import { useEffect, useMemo, useState } from "react";
import { getJson, postJson } from "../lib/api";
import { Card, Chip } from "./ui";

type EnvRow = {
  key: string; group: string; label: string; kind: string; value?: string; set?: boolean; in_file: boolean; default?: string;
  editable: boolean; restart: string[]; minimum?: number | null; maximum?: number | null; choices?: string[]; help: string;
};
type Secret = { key: string; group: string; label: string; set: boolean; cli: boolean };
type Result = { id: string; status: string; message?: string; restarted?: string; actor?: string; at?: string };
type Env = {
  groups: Record<string, string>; rows: EnvRow[]; unlisted: string[]; secrets: Secret[];
  helper: { installed: boolean; waiting: number; stuck: string[] }; requests: Result[];
  answer_lengths: { task: string; label: string; base: number; now: number }[];
};

const SECRET_GROUPS: Record<string, string> = { checks: "Checks and backup", auth: "Sign-in (LDAP / OIDC)", network: "Alarm intake", integrations: "Integrations", notifications: "Notifications", ai: "ForgeAI" };

/**
 * Settings › Server: every value in .env by part of the appliance, and every secret as set / not
 * set. Editable values go to the host apply helper, which writes .env and restarts only the
 * containers that use them; the card follows the request until the helper answers.
 */
export function ServerEnvPanel() {
  const [env, setEnv] = useState<Env | null>(null);
  const [error, setError] = useState("");
  const [draft, setDraft] = useState<Record<string, string>>({});
  const [result, setResult] = useState<Result | null>(null);
  const [busy, setBusy] = useState(false);
  const load = () => getJson<Env>("/api/v1/server/env").then((d) => (setEnv(d), setError(""))).catch((e: Error) => setError(e.message));
  useEffect(() => void load(), []);

  const changed = useMemo(() => Object.fromEntries(Object.entries(draft).filter(([k, v]) => v !== (env?.rows.find((r) => r.key === k)?.value ?? ""))), [draft, env]);
  const restarts = useMemo(() => Array.from(new Set(Object.keys(changed).flatMap((k) => env?.rows.find((r) => r.key === k)?.restart ?? []))).sort(), [changed, env]);

  const follow = async (id: string) => {
    // the helper answers within seconds; a Core restart drops a few polls, so keep trying for 3 minutes
    for (let i = 0; i < 90; i++) {
      await new Promise((r) => window.setTimeout(r, 2000));
      const r = await getJson<Result>(`/api/v1/server/env/requests/${id}`).catch(() => null);
      if (r && r.status !== "pending" && r.status !== "unknown") return r;
      if (r) setResult(r);
    }
    return { id, status: "error", message: "No answer from the apply helper in 3 minutes: ./forgesre apply-helper status" };
  };
  const apply = async () => {
    setBusy(true);
    setError("");
    try {
      const req = await postJson<{ id: string; restarts: string[] }>("/api/v1/server/env", { changes: changed });
      setResult({ id: req.id, status: "pending", message: req.restarts.length ? `Restarting ${req.restarts.join(", ")}…` : "Writing .env…" });
      const done = await follow(req.id);
      setResult(done);
      setDraft({});
      await load();
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setBusy(false);
    }
  };

  if (!env) return error ? <div className="banner error" role="alert">{error}</div> : <span className="muted">Loading…</span>;
  const canEdit = env.helper.installed;
  const groups = Object.entries(env.groups).filter(([g]) => env.rows.some((r) => r.group === g));
  return (
    <>
      <Card title="Server settings (.env)" actions={<Chip tone={canEdit ? "ok" : "off"}>{canEdit ? "Apply helper on" : "Apply helper off"}</Chip>}>
        <span className="small muted">
          Container-level values: ports, CPU threads, model context, backup folders. Core reads them at start, so a change needs the
          containers that use it restarted. With the apply helper on, <strong>Apply</strong> writes <code className="mono">.env</code> on the server
          and restarts only those containers; every change is in the audit log.
        </span>
        {!canEdit ? (
          <div className="banner warn" role="status">The apply helper is off, so these values are shown but not changed here. On the server: <code className="mono">./forgesre apply-helper on</code>, or edit <code className="mono">.env</code> and run <code className="mono">./forgesre update</code>.</div>
        ) : null}
        {env.helper.stuck.length ? <div className="banner warn" role="status">{env.helper.stuck.length} request(s) waiting: the helper does not run. <code className="mono">./forgesre apply-helper status</code></div> : null}
        {error ? <div className="banner error" role="alert">{error}</div> : null}
        {result ? (
          <div className={`banner ${result.status === "ok" ? "" : result.status === "pending" ? "warn" : "error"}`} role="status">
            <strong>{result.status === "ok" ? "Applied" : result.status === "pending" ? "Applying…" : "Not applied"}</strong> · {result.message}
          </div>
        ) : null}
      </Card>
      {groups.map(([g, title]) => (
        <Card key={g} title={title}>
          <div className="table-wrap"><table className="data" style={{ minWidth: 720 }}>
            <thead><tr><th style={{ width: "34%" }}>Setting</th><th style={{ width: 220 }}>Value</th><th>Restarts · notes</th></tr></thead>
            <tbody>
              {env.rows.filter((r) => r.group === g).map((r) => (
                <tr key={r.key}>
                  <td><div className="cell-stack"><span>{r.label}</span><span className="small mono faint">{r.key}</span></div></td>
                  <td>{r.kind === "secret" ? <Chip tone={r.set ? "ok" : "off"}>{r.set ? "set" : "not set"}</Chip>
                    : r.editable && canEdit ? (
                      r.choices && r.choices.length ? (
                        <select aria-label={r.key} value={draft[r.key] ?? r.value ?? ""} onChange={(e) => setDraft({ ...draft, [r.key]: e.target.value })}>
                          {r.choices.map((c) => <option key={c} value={c}>{c || "(off)"}</option>)}
                        </select>
                      ) : (
                        <input className="search" aria-label={r.key} value={draft[r.key] ?? r.value ?? ""} placeholder={r.default ? `default ${r.default}` : ""}
                          inputMode={r.kind === "int" || r.kind === "port" ? "numeric" : undefined} onChange={(e) => setDraft({ ...draft, [r.key]: e.target.value })} style={{ width: "100%" }} />
                      )
                    ) : <span className="mono small">{r.value || <span className="faint">{r.default ? `default ${r.default}` : "—"}</span>}</span>}
                  </td>
                  <td className="small muted">
                    {r.restart.length ? <span>restarts <strong>{r.restart.join(", ")}</strong>{r.help ? " · " : ""}</span> : null}
                    {r.help}{r.kind !== "secret" && r.minimum != null ? ` (${r.minimum}–${r.maximum})` : ""}
                    {!r.in_file && r.kind !== "secret" ? <span className="faint"> · not in .env, the default is used</span> : null}
                  </td>
                </tr>
              ))}
            </tbody>
          </table></div>
          {g === "ai" ? (
            <details>
              <summary className="small" style={{ cursor: "pointer" }}>ForgeAI answer lengths (tokens) · Settings › RCA / AI › Answer length</summary>
              <table className="data" style={{ marginTop: 8 }}>
                <thead><tr><th>Task</th><th>Built-in</th><th>Now</th></tr></thead>
                <tbody>{env.answer_lengths.map((a) => <tr key={a.task}><td>{a.label}</td><td>{a.base}</td><td>{a.now}</td></tr>)}</tbody>
              </table>
            </details>
          ) : null}
        </Card>
      ))}
      {canEdit ? (
        <div className="row" style={{ gap: 8, position: "sticky", bottom: 8 }}>
          <button className="btn primary" type="button" disabled={busy || !Object.keys(changed).length} onClick={() => void apply()}>
            {busy ? "Applying…" : `Apply ${Object.keys(changed).length || ""} change${Object.keys(changed).length === 1 ? "" : "s"}`}
          </button>
          {Object.keys(changed).length ? <span className="small muted">{restarts.length ? `Will restart: ${restarts.join(", ")} (a few seconds to a minute; the model reloads)` : "No container restart needed"}</span> : null}
          {Object.keys(changed).length ? <button className="btn" type="button" onClick={() => setDraft({})}>Undo</button> : null}
        </div>
      ) : null}
      <Card title="Secrets (secrets/secrets.env)">
        <span className="small muted">Passwords and API tokens are never shown or typed here. Replace one on the server; it is used on the next restart of Core:
          <code className="mono"> ./forgesre settings set-secret KEY</code> (asks for the value, does not echo it).</span>
        <div className="table-wrap"><table className="data">
          <thead><tr><th>Secret</th><th style={{ width: 110 }}>State</th><th>Part</th><th>Change on the server</th></tr></thead>
          <tbody>
            {env.secrets.map((s) => (
              <tr key={s.key}>
                <td><div className="cell-stack"><span>{s.label}</span><span className="small mono faint">{s.key}</span></div></td>
                <td><Chip tone={s.set ? "ok" : "off"}>{s.set ? "set" : "not set"}</Chip></td>
                <td className="small muted">{SECRET_GROUPS[s.group] ?? s.group}</td>
                <td className="small mono">{s.cli ? `./forgesre settings set-secret ${s.key}` : "set by the installer"}</td>
              </tr>
            ))}
          </tbody>
        </table></div>
      </Card>
      {env.unlisted.length ? (
        <Card title="Other values in .env">
          <span className="small muted">In the file but not described here (older or custom): {env.unlisted.join(", ")}</span>
        </Card>
      ) : null}
      {env.requests.length ? (
        <Card title="Recent changes">
          <table className="data"><thead><tr><th>When</th><th>Who</th><th>Result</th></tr></thead>
            <tbody>{env.requests.map((r) => <tr key={r.id}><td className="small">{r.at}</td><td className="small">{r.actor}</td><td className="small"><Chip tone={r.status === "ok" ? "ok" : "crit"}>{r.status}</Chip> {r.message}</td></tr>)}</tbody>
          </table>
        </Card>
      ) : null}
    </>
  );
}
