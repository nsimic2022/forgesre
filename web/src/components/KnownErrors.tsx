import { useEffect, useState, type FormEvent } from "react";
import { getJson, postJson, type KnownError } from "../lib/api";
import { Card, Chip } from "./ui";

type Draft = Omit<KnownError, "id" | "created_by" | "updated_at">;
const EMPTY: Draft = { title: "", alertname: "", pattern: "", symptoms: "", workaround: "", fix: "", link: "", status: "open" };

/** Add or edit one known error. ``initial`` with an id edits it, without one creates. */
export function KnownErrorForm({ initial, onDone, onCancel }: { initial: Partial<KnownError>; onDone: () => void; onCancel: () => void }) {
  const [d, setD] = useState<Draft>({ ...EMPTY, ...initial });
  const [error, setError] = useState("");
  const set = (k: keyof Draft, v: string) => setD((x) => ({ ...x, [k]: v }));
  const submit = async (e: FormEvent) => {
    e.preventDefault();
    setError("");
    try {
      if (initial.id) await getJson(`/api/v1/known-errors/${initial.id}`, { method: "PUT", body: JSON.stringify(d), headers: { "Content-Type": "application/json" } });
      else await postJson("/api/v1/known-errors", d);
      onDone();
    } catch (err) {
      setError((err as Error).message);
    }
  };
  const area = (k: keyof Draft, label: string, hint: string) => (
    <label className="cell-stack small faint">{label}<textarea className="search" rows={3} value={d[k]} placeholder={hint} onChange={(e) => set(k, e.target.value)} /></label>
  );
  return (
    <form className="cell-stack" style={{ gap: 8 }} onSubmit={submit} aria-label="Known error">
      {error ? <div className="banner error" role="alert">{error}</div> : null}
      <label className="cell-stack small faint">Title<input className="search" value={d.title} onChange={(e) => set("title", e.target.value)} placeholder="RAID controller cache battery learn cycle" required /></label>
      <div className="row" style={{ gap: 8, flexWrap: "wrap" }}>
        <label className="cell-stack small faint" style={{ flex: "1 1 200px" }}>Alertname<input className="search" value={d.alertname} onChange={(e) => set("alertname", e.target.value)} placeholder="NodeDiskWriteLatencyHigh" /></label>
        <label className="cell-stack small faint" style={{ flex: "1 1 200px" }}>…and/or the title contains<input className="search" value={d.pattern} onChange={(e) => set("pattern", e.target.value)} placeholder="write latency" /></label>
        <label className="cell-stack small faint">Status
          <select className="search" value={d.status} onChange={(e) => set("status", e.target.value)}><option value="open">open</option><option value="fixed">fixed</option></select>
        </label>
      </div>
      {area("symptoms", "How it shows", "What the NOC sees")}
      {area("workaround", "Workaround", "What to do now")}
      {area("fix", "Permanent fix", "Change, ticket, vendor case")}
      <label className="cell-stack small faint">Link (runbook, ticket, vendor KB)<input className="search" value={d.link} onChange={(e) => set("link", e.target.value)} placeholder="https://…" /></label>
      <div className="row" style={{ gap: 8 }}>
        <button className="btn primary" type="submit">Save</button>
        <button className="btn" type="button" onClick={onCancel}>Cancel</button>
      </div>
    </form>
  );
}

function Entry({ k }: { k: KnownError }) {
  return (
    <div className="cell-stack" style={{ gap: 4 }}>
      <span className="row" style={{ gap: 6 }}><Chip tone={k.status === "open" ? "warn" : "ok"}>{k.status === "open" ? "Known error" : "Fixed"}</Chip><strong>{k.title}</strong></span>
      {k.symptoms ? <span className="small muted">{k.symptoms}</span> : null}
      {k.workaround ? <span className="small"><span className="faint">Workaround: </span>{k.workaround}</span> : null}
      {k.fix ? <span className="small"><span className="faint">Fix: </span>{k.fix}</span> : null}
      {k.link ? <a className="small" href={k.link} target="_blank" rel="noreferrer">{k.link}</a> : null}
    </div>
  );
}

/** Incident page: known errors that match this alarm, or "Save as known error" for Engineers. */
export function IncidentKnownErrors({ items, canEdit, title, alertname, cause, onChanged }: {
  items: KnownError[]; canEdit: boolean; title: string; alertname: string; cause: string; onChanged: () => void;
}) {
  const [adding, setAdding] = useState(false);
  if (!items.length && !canEdit) return null;
  return (
    <Card title="Known errors" actions={canEdit && !adding ? <button className="btn" type="button" onClick={() => setAdding(true)}>Save as known error</button> : null}>
      {adding ? (
        <KnownErrorForm initial={{ title, alertname, workaround: cause }} onDone={() => (setAdding(false), onChanged())} onCancel={() => setAdding(false)} />
      ) : items.length ? items.map((k) => <Entry key={k.id} k={k} />) : <span className="small faint">None matches this alarm.</span>}
    </Card>
  );
}

/** Automation › Known errors: the whole knowledge base, searchable; Engineers add and edit. */
export function KnownErrorsCard() {
  const [data, setData] = useState<{ items: KnownError[]; can_edit: boolean } | null>(null);
  const [q, setQ] = useState("");
  const [edit, setEdit] = useState<Partial<KnownError> | null>(null);
  const load = () => getJson<{ items: KnownError[]; can_edit: boolean }>(`/api/v1/known-errors?q=${encodeURIComponent(q)}`).then(setData).catch(() => setData(null));
  useEffect(() => {
    const t = window.setTimeout(load, q ? 250 : 0);
    return () => window.clearTimeout(t);
  }, [q]);
  const items = Array.isArray(data?.items) ? data!.items : [];
  const remove = async (id: number) => {
    await getJson(`/api/v1/known-errors/${id}`, { method: "DELETE" }).catch(() => undefined);
    load();
  };
  return (
    <Card title="Known errors" actions={<>
      <input className="search" type="search" aria-label="Search known errors" placeholder="Search…" value={q} onChange={(e) => setQ(e.target.value)} />
      {data?.can_edit ? <button className="btn primary" type="button" onClick={() => setEdit({})}>New known error</button> : null}
    </>}>
      <span className="small faint">Recurring problems and what to do. An entry shows on every incident whose alertname and/or title matches, and ForgeAI reads it.</span>
      {edit ? <KnownErrorForm initial={edit} onDone={() => (setEdit(null), load())} onCancel={() => setEdit(null)} /> : null}
      {items.length === 0 ? <span className="muted">{q ? "Nothing matches." : "No known error yet."}</span> : null}
      {items.map((k) => (
        <div key={k.id} className="row" style={{ justifyContent: "space-between", alignItems: "flex-start", borderTop: "1px solid var(--border)", paddingTop: 10 }}>
          <div style={{ minWidth: 0 }}>
            <Entry k={k} />
            <span className="small faint">Matches: {[k.alertname, k.pattern ? `title has “${k.pattern}”` : ""].filter(Boolean).join(" and ")}</span>
          </div>
          {data?.can_edit ? (
            <span className="row" style={{ gap: 4 }}>
              <button className="btn" type="button" onClick={() => setEdit(k)}>Edit</button>
              <button className="btn" type="button" aria-label={`Delete ${k.title}`} onClick={() => void remove(k.id)}>Delete</button>
            </span>
          ) : null}
        </div>
      ))}
    </Card>
  );
}
