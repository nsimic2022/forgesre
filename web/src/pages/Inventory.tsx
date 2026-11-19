import { useEffect, useState } from "react";
import { Link } from "react-router-dom";
import { Card, Chip, Dot } from "../components/ui";
import { getJson, type AssetCard, type InventoryPage, type InventoryRow } from "../lib/api";
import { clock, severityTone, type Tone } from "../lib/format";

const PAGE = 50;
type Filters = { site: string; rack: string; type: string; environment: string; customer: string; view: string };
const EMPTY: Filters = { site: "", rack: "", type: "", environment: "", customer: "", view: "" };
const STATE: Record<InventoryRow["state"], { label: string; tone: Tone }> = {
  down: { label: "Down", tone: "crit" },
  alarm: { label: "Alarm", tone: "high" },
  up: { label: "Up", tone: "ok" },
  unknown: { label: "Not probed", tone: "off" },
};

function LibraryLink({ active, label, count, indent = 0, onClick }: { active: boolean; label: string; count: number; indent?: number; onClick: () => void }) {
  return (
    <button
      type="button"
      onClick={onClick}
      aria-pressed={active}
      className="nav-link"
      style={{ minHeight: 32, paddingLeft: 10 + indent * 14, justifyContent: "space-between", background: active ? "var(--s2)" : "none", border: 0, width: "100%", fontSize: 13, cursor: "pointer", color: active ? "var(--text)" : "var(--muted)" }}
    >
      <span>{label}</span>
      <span className="small faint">{count}</span>
    </button>
  );
}

function Library({ data, filters, set }: { data: InventoryPage["library"]; filters: Filters; set: (f: Filters) => void }) {
  const only = (patch: Partial<Filters>) => set({ ...EMPTY, ...patch });
  const heading = (text: string) => <h3 className="small faint" style={{ margin: "6px 0 2px", textTransform: "uppercase", letterSpacing: ".05em" }}>{text}</h3>;
  return (
    <aside className="card" aria-label="Asset library" style={{ flex: "1 1 220px", maxWidth: 280, gap: 4 }}>
      <LibraryLink active={!Object.values(filters).some(Boolean)} label="All assets" count={data.sites.reduce((n, s) => n + s.count, 0)} onClick={() => set(EMPTY)} />
      {heading("By location")}
      {data.sites.map((site) => (
        <div key={site.name}>
          <LibraryLink active={filters.site === site.name && !filters.rack} label={site.name} count={site.count} onClick={() => only({ site: site.name })} />
          {site.racks.map((rack) => (
            <LibraryLink key={rack.name} indent={1} active={filters.site === site.name && filters.rack === rack.name} label={rack.name} count={rack.count} onClick={() => only({ site: site.name, rack: rack.name })} />
          ))}
        </div>
      ))}
      {heading("By type")}
      {data.types.map((t) => <LibraryLink key={t.name} active={filters.type === t.name} label={t.name} count={t.count} onClick={() => only({ type: t.name })} />)}
      {heading("By environment")}
      {data.environments.map((t) => <LibraryLink key={t.name} active={filters.environment === t.name} label={t.name} count={t.count} onClick={() => only({ environment: t.name })} />)}
      {data.customers.length ? heading("By customer") : null}
      {data.customers.map((t) => <LibraryLink key={t.name} active={filters.customer === t.name} label={t.name} count={t.count} onClick={() => only({ customer: t.name })} />)}
      {heading("Views")}
      {data.views.map((v) => <LibraryLink key={v.key} active={filters.view === v.key} label={v.name} count={v.count} onClick={() => only({ view: v.key })} />)}
    </aside>
  );
}

function AssetPanel({ assetId, onClose }: { assetId: string; onClose: () => void }) {
  const [card, setCard] = useState<AssetCard | null>(null);
  const [error, setError] = useState("");
  useEffect(() => {
    setCard(null);
    getJson<AssetCard>(`/api/v1/inventory/${encodeURIComponent(assetId)}`).then(setCard).catch((e: Error) => setError(e.message));
  }, [assetId]);
  if (error) return <Card><div className="banner error" role="alert">{error}</div></Card>;
  if (!card) return <Card><span className="muted">Loading…</span></Card>;
  const st = STATE[card.state];
  const dl = (rows: [string, string][]) => (
    <dl style={{ margin: 0, display: "grid", gridTemplateColumns: "120px minmax(0,1fr)", gap: "6px 12px", fontSize: 13 }}>
      {rows.filter(([, v]) => v).map(([k, v]) => (
        <div key={k} style={{ display: "contents" }}>
          <dt className="faint">{k}</dt>
          <dd style={{ margin: 0, minWidth: 0, overflowWrap: "anywhere" }}>{v}</dd>
        </div>
      ))}
    </dl>
  );
  return (
    <Card
      title={card.hostname}
      actions={
        <>
          <Chip tone={st.tone}>{st.label}</Chip>
          <button className="btn" onClick={onClose} aria-label="Close asset card">Close</button>
        </>
      }
    >
      <span className="small muted">{[card.type, card.ip, card.environment].filter(Boolean).join(" · ")}</span>
      <h3 className="small faint" style={{ margin: 0, textTransform: "uppercase", letterSpacing: ".05em" }}>Location and owner</h3>
      {dl([["Site", card.site], ["Row / rack", [card.row, card.rack].filter(Boolean).join(" / ")], ["Customer", card.customer], ["Owner", card.owner], ["Contact", [card.contact_name, card.owner_phone, card.owner_email].filter(Boolean).join(" · ")], ["Uplink / parent", card.parent]])}
      {card.details.length ? (
        <>
          <h3 className="small faint" style={{ margin: 0, textTransform: "uppercase", letterSpacing: ".05em" }}>Support and runbook</h3>
          {dl(card.details.filter((d) => !["site", "row", "rack", "customer"].includes(d.key)).map((d) => [d.label, d.value] as [string, string]))}
        </>
      ) : null}
      <h3 className="small faint" style={{ margin: 0, textTransform: "uppercase", letterSpacing: ".05em" }}>Monitoring sources</h3>
      <div className="row">{card.sources.length ? card.sources.map((s) => <Chip key={s} tone="ok">{s}</Chip>) : <span className="muted">None: nothing watches this asset.</span>}</div>
      {dl([["Scrape", card.scrape_address], ["Profile", card.monitoring_profile], ["GLPI", card.glpi_id && `#${card.glpi_id}`], ["Zabbix", card.zabbix_hostid]])}
      <h3 className="small faint" style={{ margin: 0, textTransform: "uppercase", letterSpacing: ".05em" }}>Recent incidents</h3>
      {card.incidents.length === 0 ? <span className="muted">None.</span> : null}
      {card.incidents.map((i) => (
        <Link key={i.number} to={`/incidents/${encodeURIComponent(i.number)}`} className="row" style={{ justifyContent: "space-between", color: "var(--text)" }}>
          <span className="row" style={{ gap: 8 }}><Dot tone={severityTone(i.severity)} />{i.title}</span>
          <span className="small faint">{clock(i.started_at)} · {i.status}</span>
        </Link>
      ))}
      {card.notes ? <p className="small muted" style={{ margin: 0 }}>{card.notes}</p> : null}
      <div className="row">
        {card.can_edit ? <a className="btn" href={`/assets/${encodeURIComponent(card.asset_id)}`}>Edit (classic)</a> : null}
      </div>
    </Card>
  );
}

export function InventoryPage() {
  const [filters, setFilters] = useState<Filters>(EMPTY);
  const [query, setQuery] = useState("");
  const [offset, setOffset] = useState(0);
  const [data, setData] = useState<InventoryPage | null>(null);
  const [selected, setSelected] = useState("");
  const [error, setError] = useState("");

  useEffect(() => {
    const params = new URLSearchParams({ ...filters, q: query, limit: String(PAGE), offset: String(offset) });
    const timer = window.setTimeout(() => {
      getJson<InventoryPage>(`/api/v1/inventory?${params}`)
        .then((next) => (setData(next), setError("")))
        .catch((e: Error) => setError(e.message));
    }, query ? 250 : 0);
    return () => window.clearTimeout(timer);
  }, [filters, query, offset]);

  const set = (next: Filters) => (setFilters(next), setOffset(0));
  return (
    <>
      <header className="page-head">
        <div>
          <h1>Inventory</h1>
          <span className="muted">One record per asset · {data ? `${data.all} assets` : "loading…"}</span>
        </div>
        <div className="row">
          <a className="btn" href="/discovery">Discovery (classic)</a>
          <a className="btn" href="/assets">Add or import (classic)</a>
        </div>
      </header>
      {error ? <div className="banner error" role="alert">{error}</div> : null}
      <div className="cols">
        {data ? <Library data={data.library} filters={filters} set={set} /> : null}
        <div style={{ flex: "999 1 560px", minWidth: 0 }}>
          <Card>
            <div className="row">
              <input className="search" type="search" aria-label="Search assets" placeholder="Hostname, IP, owner, rack, customer…" value={query} onChange={(e) => (setQuery(e.target.value), setOffset(0))} />
              {Object.entries(filters).filter(([, v]) => v).map(([k, v]) => (
                <button key={k} className="btn" onClick={() => set({ ...filters, [k]: "" })} aria-label={`Remove filter ${k}`}>{v} ×</button>
              ))}
            </div>
            <div className="table-wrap">
              <table className="data" style={{ minWidth: 980 }}>
                <thead>
                  <tr>
                    <th style={{ width: 100 }}>State</th>
                    <th style={{ width: 170 }}>Hostname</th>
                    <th style={{ width: 130 }}>IP</th>
                    <th style={{ width: 140 }}>Type</th>
                    <th style={{ width: 150 }}>Location</th>
                    <th style={{ width: 120 }}>Owner</th>
                    <th>Sources</th>
                    <th style={{ width: 64 }}>Open</th>
                  </tr>
                </thead>
                <tbody>
                  {(data?.rows ?? []).map((row) => {
                    const st = STATE[row.state];
                    return (
                      <tr key={row.asset_id} style={selected === row.asset_id ? { background: "var(--accent-bg)" } : undefined}>
                        <td><span className="row" style={{ gap: 8 }}><Dot tone={st.tone} /><span className="small muted">{st.label}</span></span></td>
                        <td>
                          <button type="button" onClick={() => setSelected(row.asset_id)} style={{ background: "none", border: 0, padding: 0, color: "var(--text)", fontWeight: 600, cursor: "pointer", font: "inherit" }}>
                            {row.hostname}
                          </button>
                        </td>
                        <td className="mono muted">{row.ip}</td>
                        <td>{row.type}</td>
                        <td className="small">{[row.site, row.rack].filter(Boolean).join(" › ") || <span className="faint">—</span>}</td>
                        <td className="small">{row.owner || <span className="faint">—</span>}</td>
                        <td><div className="row" style={{ gap: 4 }}>{row.sources.map((s) => <Chip key={s} tone="off">{s}</Chip>)}</div></td>
                        <td>{row.open ? <Chip tone="crit">{row.open}</Chip> : <span className="faint">—</span>}</td>
                      </tr>
                    );
                  })}
                </tbody>
              </table>
              {data && data.rows.length === 0 ? <div className="empty">No assets match.</div> : null}
            </div>
            {data && data.total > PAGE ? (
              <div className="row" style={{ justifyContent: "space-between" }}>
                <span className="small faint">{offset + 1}–{Math.min(offset + PAGE, data.total)} of {data.total}</span>
                <div className="row">
                  <button className="btn" disabled={offset === 0} onClick={() => setOffset(Math.max(0, offset - PAGE))}>Previous</button>
                  <button className="btn" disabled={offset + PAGE >= data.total} onClick={() => setOffset(offset + PAGE)}>Next</button>
                </div>
              </div>
            ) : null}
          </Card>
        </div>
        {selected ? (
          <div style={{ flex: "1 1 340px", minWidth: 0 }}>
            <AssetPanel assetId={selected} onClose={() => setSelected("")} />
          </div>
        ) : null}
      </div>
    </>
  );
}
