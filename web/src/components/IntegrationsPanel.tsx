import { useEffect, useState } from "react";
import { getJson, postJson, type SettingRow } from "../lib/api";
import { clock } from "../lib/format";
import { ReadOnlyRow, SettingField, TestButton, tone } from "./SettingControls";
import { Card, Chip } from "./ui";

export type IntegrationCard = {
  id: string; label: string; role: string; kind: "source" | "stack";
  status: string; why: string; links: { label: string; url: string }[]; local_only: boolean; local_hint: string;
  settings: string[]; test: string; sync: string; last_sync: { at: string | null; status: string; summary: string } | null;
};
type Data = { sources: IntegrationCard[]; stack: IntegrationCard[] };

function OneCard({ card, rows, onSaved }: { card: IntegrationCard; rows: SettingRow[]; onSaved: (row: SettingRow) => void }) {
  const [open, setOpen] = useState(false);
  const [sync, setSync] = useState<"idle" | "queued" | string>("idle");
  const mine = card.settings.map((k) => rows.find((r) => r.key === k)).filter((r): r is SettingRow => Boolean(r));
  const runSync = () => postJson(`/api/v1/integrations/${card.sync}/sync`, {}).then(() => setSync("queued")).catch((e: Error) => setSync(e.message));
  return (
    <section className="card" style={{ gap: 10, minWidth: 0 }} aria-label={card.label}>
      <div className="row" style={{ justifyContent: "space-between" }}>
        <h3 style={{ margin: 0, fontSize: 15 }}>{card.label}</h3>
        <Chip tone={tone(card.status)}>{card.status}</Chip>
      </div>
      <span className="small muted">{card.role}</span>
      {card.why ? <span className="small" style={{ overflowWrap: "anywhere" }}>{card.why}</span> : null}
      {card.last_sync ? (
        <span className="small faint">
          Last sync {clock(card.last_sync.at)} · <span style={{ color: card.last_sync.status === "ok" ? "var(--ok)" : "var(--crit)" }}>{card.last_sync.status}</span>
          {card.last_sync.summary ? ` · ${card.last_sync.summary}` : ""}
        </span>
      ) : card.sync ? <span className="small faint">Not synced yet.</span> : null}
      {card.local_only ? <span className="small faint">{card.local_hint}</span> : null}
      <div className="row" style={{ gap: 6, flexWrap: "wrap" }}>
        {card.links.map((l) => (
          <a key={l.url} className="btn" href={l.url} target="_blank" rel="noopener noreferrer">Open {l.label}</a>
        ))}
        {card.test ? <TestButton component={card.test} label="Test connection" known={card.why} /> : null}
        {card.sync ? (
          <button className="btn" type="button" disabled={sync === "queued"} onClick={runSync}>{sync === "queued" ? "Sync queued" : "Sync now"}</button>
        ) : null}
        {mine.length ? <button className="btn" type="button" aria-expanded={open} onClick={() => setOpen(!open)}>{open ? "Hide settings" : `Settings (${mine.length})`}</button> : null}
      </div>
      {sync !== "idle" && sync !== "queued" ? <span className="small" role="alert" style={{ color: "var(--crit)" }}>{sync}</span> : null}
      {open ? (
        <div>
          {mine.map((row) => (row.editable ? <SettingField key={row.key} row={row} onSaved={onSaved} compact /> : <ReadOnlyRow key={row.key} row={row} compact />))}
        </div>
      ) : null}
    </section>
  );
}

/** Settings › Integrations: each outside system and each part of the monitoring stack as a card,
 *  with status, a link to its own UI, Test, Sync now and its settings in place. */
export function IntegrationsPanel({ rows, onSaved }: { rows: SettingRow[]; onSaved: (row: SettingRow) => void }) {
  const [data, setData] = useState<Data | null>(null);
  const [error, setError] = useState("");
  useEffect(() => {
    getJson<Data>("/api/v1/integrations").then(setData).catch((e: Error) => setError(e.message));
  }, []);
  const grid = { display: "grid", gridTemplateColumns: "repeat(auto-fill, minmax(300px, 1fr))", gap: 16 } as const;
  return (
    <>
      {error ? <div className="banner error" role="alert">{error}</div> : null}
      <Card title="Sources ForgeSRE reads from">
        <span className="small faint">
          Every integration only reads. A sync runs in the background and its result shows here and in the journal; a failing
          integration never stops alarms coming in. Tokens are set on the server with <code className="mono">./forgesre settings set-secret NAME</code> and never shown.
        </span>
        <div style={grid}>{(data?.sources ?? []).map((c) => <OneCard key={c.id} card={c} rows={rows} onSaved={onSaved} />)}</div>
        {!data && !error ? <span className="muted">Loading…</span> : null}
      </Card>
      <Card title="Monitoring stack on this appliance">
        <span className="small faint">
          Links open each service&apos;s own page. Services that listen only on the appliance (127.0.0.1) show how to reach them over an SSH tunnel instead.
        </span>
        <div style={grid}>{(data?.stack ?? []).map((c) => <OneCard key={c.id} card={c} rows={rows} onSaved={onSaved} />)}</div>
      </Card>
    </>
  );
}
