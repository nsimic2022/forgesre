import { useEffect, useState, type FormEvent } from "react";
import { Card, Chip } from "../components/ui";
import { getJson, postJson, putJson, type OncallSlot, type OncallWeek } from "../lib/api";
import { IncidentsNav } from "./Incidents";

const HOURS = [0, 6, 12, 18, 24];
const TRACK = 420; // px for 24 h

type Draft = { weekday: number; start: string; end: string; name: string; surname: string; email: string; phone: string; phone2: string; alsoDays: number[] };
const EMPTY: Draft = { weekday: 0, start: "08:00", end: "16:00", name: "", surname: "", email: "", phone: "", phone2: "", alsoDays: [] };

function toDraft(s: OncallSlot): Draft {
  return { weekday: s.weekday, start: s.start, end: s.end, name: s.name, surname: s.surname, email: s.email, phone: s.phone, phone2: s.phone2, alsoDays: [] };
}

function SlotForm({ week, slot, copy, onDone }: { week: OncallWeek; slot: OncallSlot | null; copy: boolean; onDone: (saved: boolean) => void }) {
  const [d, setD] = useState<Draft>(slot ? toDraft(slot) : EMPTY);
  const [error, setError] = useState("");
  const set = (patch: Partial<Draft>) => setD({ ...d, ...patch });
  const editing = slot !== null && !copy;
  const pick = (label: string) => {
    const p = week.people.find((x) => x.label === label);
    set(p ? { name: p.name, surname: p.surname, email: p.email, phone: p.phone, phone2: p.phone2 } : { name: label });
  };
  const save = async (e: FormEvent) => {
    e.preventDefault();
    setError("");
    const { alsoDays, ...body } = d;
    try {
      if (editing) await putJson(`/api/v1/oncall-slots/${slot.id}`, body);
      else for (const day of [d.weekday, ...alsoDays.filter((x) => x !== d.weekday)]) await postJson("/api/v1/oncall-slots", { ...body, weekday: day });
      onDone(true);
    } catch (err) {
      setError((err as Error).message);
    }
  };
  const text = (key: keyof Draft, label: string, type = "text", list?: string, onChange?: (v: string) => void) => (
    <label className="cell-stack small faint" style={{ flex: "1 1 160px" }}>
      {label}
      <input className="search" style={{ minWidth: 0 }} type={type} list={list} value={String(d[key])} onChange={(e) => (onChange ?? ((v: string) => set({ [key]: v } as Partial<Draft>)))(e.target.value)} />
    </label>
  );
  return (
    <Card title={editing ? `Edit on-call · ${slot.window}` : copy ? "Copy on-call window" : "Add on-call window"}>
      <form onSubmit={save} className="cell-stack" style={{ gap: 12 }}>
        <div className="row" style={{ alignItems: "flex-end" }}>
          <label className="cell-stack small faint" style={{ flex: "1 1 160px" }}>
            Day
            <select className="search" style={{ minWidth: 0 }} value={d.weekday} onChange={(e) => set({ weekday: Number(e.target.value) })}>
              {week.weekdays.map((name, i) => <option key={name} value={i}>{name}</option>)}
            </select>
          </label>
          {text("start", "From", "time")}
          {text("end", "Until", "time")}
        </div>
        {!editing ? (
          <fieldset style={{ border: 0, padding: 0, margin: 0 }}>
            <legend className="small faint">Also add the same window on</legend>
            <div className="row">
              {week.weekdays.map((name, i) => i === d.weekday ? null : (
                <label key={name} className="btn">
                  <input type="checkbox" checked={d.alsoDays.includes(i)} onChange={() => set({ alsoDays: d.alsoDays.includes(i) ? d.alsoDays.filter((x) => x !== i) : [...d.alsoDays, i] })} /> {name.slice(0, 3)}
                </label>
              ))}
            </div>
          </fieldset>
        ) : null}
        <div className="row" style={{ alignItems: "flex-end" }}>
          {text("name", "Name", "text", "oncall-people", pick)}
          {text("surname", "Surname")}
          {text("email", "Email", "email")}
        </div>
        <div className="row" style={{ alignItems: "flex-end" }}>
          {text("phone", "Phone", "tel")}
          {text("phone2", "Second phone", "tel")}
        </div>
        <datalist id="oncall-people">{week.people.map((p) => <option key={p.email} value={p.label} />)}</datalist>
        {error ? <div className="banner error" role="alert">{error}</div> : null}
        <div className="row">
          <button className="btn primary" type="submit">{editing ? "Save window" : "Add window"}</button>
          <button className="btn" type="button" onClick={() => onDone(false)}>Cancel</button>
        </div>
      </form>
    </Card>
  );
}

function Week({ week, onPick }: { week: OncallWeek; onPick: (s: OncallSlot) => void }) {
  return (
    <div className="table-wrap">
      <div style={{ display: "grid", gridTemplateColumns: "44px repeat(7, minmax(120px, 1fr))", gap: 8, minWidth: 940 }}>
        <span />
        {week.weekdays.map((d) => <strong key={d} className="small" style={{ textAlign: "center" }}>{d}</strong>)}
        <div style={{ position: "relative", height: TRACK }} aria-hidden="true">
          {HOURS.map((h) => <span key={h} className="small faint mono" style={{ position: "absolute", top: (h / 24) * TRACK - 8, right: 4 }}>{String(h).padStart(2, "0")}</span>)}
        </div>
        {week.weekdays.map((dayName, day) => (
          <div key={dayName} role="list" aria-label={`${dayName} on-call`} style={{ position: "relative", height: TRACK, background: "var(--s2)", border: "1px solid var(--border)", borderRadius: 8 }}>
            {week.slots.filter((s) => s.weekday === day).map((s) => {
              const now = week.now?.id === s.id;
              return (
                <button
                  key={s.id}
                  role="listitem"
                  type="button"
                  onClick={() => onPick(s)}
                  aria-label={`${s.window} ${s.person}${s.enabled ? "" : " (off)"}`}
                  style={{
                    position: "absolute", left: 4, right: 4, top: (s.start_minute / 1440) * TRACK, minHeight: 26,
                    height: Math.max(26, ((s.end_minute - s.start_minute) / 1440) * TRACK), overflow: "hidden", textAlign: "left",
                    padding: "4px 6px", borderRadius: 6, cursor: "pointer", font: "inherit", fontSize: 12,
                    background: s.enabled ? (now ? "var(--ok-bg)" : "var(--accent-bg)") : "var(--off-bg)",
                    color: s.enabled ? "var(--text)" : "var(--muted)",
                    border: `1px solid ${now ? "var(--ok)" : s.enabled ? "var(--accent)" : "var(--border)"}`,
                  }}
                >
                  <span className="mono" style={{ display: "block" }}>{s.start}–{s.end}</span>
                  <span style={{ fontWeight: 600 }}>{s.person}</span>
                </button>
              );
            })}
          </div>
        ))}
      </div>
    </div>
  );
}

/** On-call tab: who is on call now, the week grid and roster editing (Engineer+). */
export function OnCallPage() {
  const [week, setWeek] = useState<OncallWeek | null>(null);
  const [error, setError] = useState("");
  const [picked, setPicked] = useState<OncallSlot | null>(null);
  const [form, setForm] = useState<{ slot: OncallSlot | null; copy: boolean } | null>(null);
  const [asking, setAsking] = useState(false);
  const load = () => getJson<OncallWeek>("/api/v1/oncall-week").then((w) => (setWeek(w), setError(""))).catch((e: Error) => setError(e.message));
  useEffect(() => {
    load();
  }, []);
  const act = (call: Promise<unknown>) => call.then(() => (setPicked(null), setAsking(false), load())).catch((e: Error) => setError(e.message));
  const done = (saved: boolean) => (setForm(null), setPicked(null), saved && load());
  return (
    <>
      <header className="page-head">
        <div>
          <h1>On-call</h1>
          <span className="muted">Who answers, every day of the week{week?.timezone ? ` · times in ${week.timezone}` : ""}</span>
        </div>
        {week?.can_edit ? (
          <div className="row">
            <button className="btn" type="button" onClick={() => act(postJson("/api/v1/oncall-slots/take", {}))}>Take the rest of today</button>
            <button className="btn primary" type="button" onClick={() => (setPicked(null), setForm({ slot: null, copy: false }))}>New window</button>
          </div>
        ) : null}
      </header>
      <IncidentsNav />
      {error ? <div className="banner error" role="alert">{error}</div> : null}
      <Card title="On call now">
        {week?.now ? (
          <div className="cell-stack">
            <span style={{ fontWeight: 600 }}>{week.now.person}</span>
            <span className="small muted">{[week.now.window, week.now.phone, week.now.email].filter(Boolean).join(" · ")}</span>
          </div>
        ) : (
          <span className="muted">{week ? "Nobody on the roster for this minute. Escalation to “oncall” falls back to the asset owner." : "Loading…"}</span>
        )}
      </Card>
      {form && week ? <SlotForm key={`${form.slot?.id ?? "new"}-${form.copy}`} week={week} slot={form.slot} copy={form.copy} onDone={done} /> : null}
      {picked && !form ? (
        <Card title={`${picked.window} · ${picked.person}`} actions={<Chip tone={picked.enabled ? "ok" : "off"}>{picked.enabled ? "On" : "Off"}</Chip>}>
          <span className="small muted">{[picked.email, picked.phone, picked.phone2].filter(Boolean).join(" · ")}</span>
          <div className="row">
            {week?.can_edit ? (
              <>
                <button className="btn primary" type="button" onClick={() => setForm({ slot: picked, copy: false })}>Edit</button>
                <button className="btn" type="button" onClick={() => setForm({ slot: picked, copy: true })}>Copy</button>
                <button className="btn" type="button" onClick={() => act(postJson(`/api/v1/oncall-slots/${picked.id}/toggle`, {}))}>{picked.enabled ? "Turn off" : "Turn on"}</button>
                {asking ? (
                  <>
                    <button className="btn" type="button" style={{ borderColor: "var(--crit)", color: "var(--crit)" }} onClick={() => act(getJson(`/api/v1/oncall-slots/${picked.id}`, { method: "DELETE" }))}>Yes, remove</button>
                    <button className="btn" type="button" onClick={() => setAsking(false)}>Keep</button>
                  </>
                ) : (
                  <button className="btn" type="button" onClick={() => setAsking(true)}>Remove</button>
                )}
              </>
            ) : null}
            <button className="btn" type="button" onClick={() => (setPicked(null), setAsking(false))}>Close</button>
          </div>
        </Card>
      ) : null}
      {week ? (
        <Card title="Week" actions={<span className="small faint">{week.slots.length} windows · click one to change it</span>}>
          <Week week={week} onPick={(s) => (setForm(null), setAsking(false), setPicked(s))} />
          {week.slots.length === 0 ? <div className="empty">No on-call windows yet.{week.can_edit ? " Use New window to add the first one." : ""}</div> : null}
        </Card>
      ) : null}
    </>
  );
}
