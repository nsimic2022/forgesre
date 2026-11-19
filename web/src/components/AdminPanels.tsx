import { useEffect, useState, type FormEvent } from "react";
import { getJson, postJson } from "../lib/api";
import { clock } from "../lib/format";
import { Card, Chip } from "./ui";

type UserRow = { id: number; email: string; email2: string; name: string; surname: string; phone: string; phone2: string; role: string; active: boolean; me: boolean; last_admin: boolean; delete_blocked: string };
type Role = { key: string; label: string; help: string };
type UsersData = { users: UserRow[]; roles: Role[] };
const FIELDS: [keyof UserRow, string, string][] = [["name", "Name", "text"], ["surname", "Surname", "text"], ["email", "Email (sign-in)", "email"], ["phone", "Phone", "tel"], ["email2", "Second email", "email"], ["phone2", "Second phone", "tel"]];

function UserForm({ row, roles, onDone }: { row: UserRow | null; roles: Role[]; onDone: (saved: boolean) => void }) {
  const [d, setD] = useState<Record<string, string>>(() => ({
    name: row?.name ?? "", surname: row?.surname ?? "", email: row?.email ?? "", phone: row?.phone ?? "", email2: row?.email2 ?? "", phone2: row?.phone2 ?? "",
    role: row?.role ?? "analyst", password: "",
  }));
  const [error, setError] = useState("");
  const save = (e: FormEvent) => {
    e.preventDefault();
    const body: Record<string, string> = { ...d };
    if (row && !body.password) delete body.password;
    postJson(row ? `/api/v1/users/${row.id}` : "/api/v1/users", body).then(() => onDone(true)).catch((err: Error) => setError(err.message));
  };
  return (
    <Card title={row ? `Edit ${row.email}` : "New user"}>
      <form onSubmit={save} className="cell-stack" style={{ gap: 12 }}>
        <div className="row" style={{ alignItems: "flex-end" }}>
          {FIELDS.map(([key, label, type]) => (
            <label key={key} className="cell-stack small faint" style={{ flex: "1 1 200px" }}>
              {label}
              <input className="search" style={{ minWidth: 0 }} type={type} required={key === "name" || key === "email"} value={d[key]} onChange={(e) => setD({ ...d, [key]: e.target.value })} />
            </label>
          ))}
        </div>
        <fieldset style={{ border: 0, padding: 0, margin: 0 }}>
          <legend className="small faint">Role (each one can do everything the one above it can)</legend>
          <div className="cell-stack" style={{ gap: 6 }}>
            {roles.map((r) => (
              <label key={r.key} className="row small" style={{ gap: 8 }}>
                <input type="radio" name="role" checked={d.role === r.key} disabled={row?.last_admin && r.key !== "admin"} onChange={() => setD({ ...d, role: r.key })} />
                <span>{r.help}</span>
              </label>
            ))}
          </div>
          {row?.last_admin ? <span className="small faint">This is the last Admin, so the role cannot be lowered.</span> : null}
        </fieldset>
        <label className="cell-stack small faint" style={{ maxWidth: 320 }}>
          {row ? "New password (empty = keep; a change signs the user out everywhere)" : "Password"}
          <input className="search" style={{ minWidth: 0 }} type="password" autoComplete="new-password" required={!row} value={d.password} onChange={(e) => setD({ ...d, password: e.target.value })} />
        </label>
        {error ? <div className="banner error" role="alert">{error}</div> : null}
        <div className="row">
          <button className="btn primary" type="submit">{row ? "Save user" : "Create user"}</button>
          <button className="btn" type="button" onClick={() => onDone(false)}>Cancel</button>
        </div>
      </form>
    </Card>
  );
}

/** Settings › Users and roles (Admin). Server checks every write; last Admin and yourself cannot be removed. */
export function UsersPanel() {
  const [data, setData] = useState<UsersData | null>(null);
  const [edit, setEdit] = useState<{ row: UserRow | null } | null>(null);
  const [asking, setAsking] = useState<number | null>(null);
  const [error, setError] = useState("");
  const load = () => getJson<UsersData>("/api/v1/users-page").then(setData).catch((e: Error) => setError(e.message));
  useEffect(() => {
    load();
  }, []);
  const remove = (id: number) => postJson(`/api/v1/users/${id}/delete`, {}).then(() => (setAsking(null), load())).catch((e: Error) => setError(e.message));
  return (
    <>
      {edit && data ? <UserForm key={edit.row?.id ?? "new"} row={edit.row} roles={data.roles} onDone={(saved) => (setEdit(null), saved && load())} /> : null}
      <Card title="Users and roles" actions={<button className="btn primary" type="button" onClick={() => setEdit({ row: null })}>New user</button>}>
        {error ? <div className="banner error" role="alert">{error}</div> : null}
        <div className="table-wrap">
          <table className="data" style={{ minWidth: 820 }}>
            <thead><tr><th>Name</th><th>Email</th><th>Phone</th><th style={{ width: 110 }}>Role</th><th style={{ width: 260 }}>Change</th></tr></thead>
            <tbody>
              {(data?.users ?? []).map((u) => (
                <tr key={u.id}>
                  <td style={{ fontWeight: 600 }}>{[u.name, u.surname].filter(Boolean).join(" ")}{u.me ? <span className="small faint"> (you)</span> : null}</td>
                  <td className="small">{u.email}</td>
                  <td className="small muted">{u.phone}</td>
                  <td><Chip tone={u.role === "admin" ? "high" : u.role === "engineer" ? "info" : u.role === "analyst" ? "ok" : "off"}>{data?.roles.find((r) => r.key === u.role)?.label ?? u.role}</Chip></td>
                  <td>
                    <div className="row" style={{ gap: 6 }}>
                      <button className="btn" type="button" onClick={() => setEdit({ row: u })} aria-label={`Edit user ${u.email}`}>Edit</button>
                      {u.delete_blocked ? <span className="small faint">{u.delete_blocked}</span> : asking === u.id ? (
                        <>
                          <button className="btn" type="button" style={{ borderColor: "var(--crit)", color: "var(--crit)" }} onClick={() => remove(u.id)}>Yes, remove</button>
                          <button className="btn" type="button" onClick={() => setAsking(null)}>Keep</button>
                        </>
                      ) : (
                        <button className="btn" type="button" onClick={() => setAsking(u.id)} aria-label={`Remove user ${u.email}`}>Remove</button>
                      )}
                    </div>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </Card>
    </>
  );
}

type Archive = { name: string; size: number; mtime: string; label: string };
type BackupData = { archives: Archive[]; running: boolean; last_job: null | { status: string; error: string; result: string } };

/** Settings › Backup (Admin): make a backup in the background, download, upload, remove. Restore stays a confirmed step on the classic page. */
export function BackupPanel() {
  const [data, setData] = useState<BackupData | null>(null);
  const [secrets, setSecrets] = useState(true);
  const [models, setModels] = useState(false);
  const [asking, setAsking] = useState("");
  const [notice, setNotice] = useState("");
  const [error, setError] = useState("");
  const load = () => getJson<BackupData>("/api/v1/backups").then(setData).catch((e: Error) => setError(e.message));
  useEffect(() => {
    load();
  }, []);
  useEffect(() => {
    if (!data?.running) return;
    const timer = window.setTimeout(load, 3000);
    return () => window.clearTimeout(timer);
  }, [data]);
  const start = () => postJson("/api/v1/backups", { include_secrets: secrets, include_models: models }).then(() => (setNotice("Backup started. It appears below when done."), load())).catch((e: Error) => setError(e.message));
  const upload = (file: File | undefined) => {
    if (!file) return;
    const form = new FormData();
    form.append("archive", file);
    getJson<{ imported: string }>("/api/v1/backups/import", { method: "POST", body: form }).then((r) => (setNotice(`Imported ${r.imported}.`), load())).catch((e: Error) => setError(e.message));
  };
  const remove = (name: string) => getJson(`/api/v1/backups/${encodeURIComponent(name)}`, { method: "DELETE" }).then(() => (setAsking(""), load())).catch((e: Error) => setError(e.message));
  return (
    <Card title="Backup, import, export" actions={<a className="btn" href="/admin#backup">Restore (classic, asks to confirm)</a>}>
      <div className="row">
        <label className="row small"><input type="checkbox" checked={secrets} onChange={(e) => setSecrets(e.target.checked)} /> Include secrets (keep the file on trusted storage)</label>
        <label className="row small"><input type="checkbox" checked={models} onChange={(e) => setModels(e.target.checked)} /> Include the LLM model (large)</label>
        <button className="btn primary" type="button" disabled={data?.running} onClick={start}>{data?.running ? "Backup running…" : "Back up now"}</button>
        <label className="btn">Upload a backup<input type="file" accept=".tar,.gz,.tgz,.tar.gz" style={{ display: "none" }} onChange={(e) => upload(e.target.files?.[0])} /></label>
      </div>
      {notice ? <div className="banner" role="status" style={{ background: "var(--ok-bg)" }}>{notice}</div> : null}
      {error ? <div className="banner error" role="alert">{error}</div> : null}
      {data?.last_job?.status === "error" ? <div className="banner error" role="alert">Last backup failed: {data.last_job.error}</div> : null}
      <div className="table-wrap">
        <table className="data">
          <thead><tr><th>Backup</th><th style={{ width: 170 }}>Made</th><th style={{ width: 240 }}>Change</th></tr></thead>
          <tbody>
            {(data?.archives ?? []).map((a) => (
              <tr key={a.name}>
                <td className="mono small">{a.label}</td>
                <td className="small muted">{clock(a.mtime)}</td>
                <td>
                  <div className="row" style={{ gap: 6 }}>
                    <a className="btn" href={`/admin/backups/${encodeURIComponent(a.name)}`}>Download</a>
                    {asking === a.name ? (
                      <>
                        <button className="btn" type="button" style={{ borderColor: "var(--crit)", color: "var(--crit)" }} onClick={() => remove(a.name)}>Yes, remove</button>
                        <button className="btn" type="button" onClick={() => setAsking("")}>Keep</button>
                      </>
                    ) : (
                      <button className="btn" type="button" onClick={() => setAsking(a.name)} aria-label={`Remove backup ${a.name}`}>Remove</button>
                    )}
                  </div>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
        {data && data.archives.length === 0 ? <div className="empty">No backups yet.</div> : null}
      </div>
      <span className="small faint">Same as <code className="mono">./forgesre backup</code> and <code className="mono">./forgesre restore</code> on the host.</span>
    </Card>
  );
}
