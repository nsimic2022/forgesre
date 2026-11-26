import { MonitoringCheck } from "../components/MonitoringCheck";
import { useEffect, useRef, useState } from "react";
import type React from "react";
import { Link } from "react-router-dom";
import { AssetEditor, type EditorMode } from "../components/AssetEditor";
import { IncidentMetrics } from "../components/IncidentPanels";
import { CapacityCard } from "../components/CapacityCard";
import { InventoryTree } from "../components/InventoryTree";
import { ImportCsv } from "../components/ImportCsv";
import { SourcesPanel } from "../components/SourcesPanel";
import { Pager, usePageSize } from "../components/Pager";
import { Card, Chip, Dot } from "../components/ui";
import { getJson, postJson, type AssetCard, type AssetFormOptions, type InventoryPage, type InventoryRow } from "../lib/api";
import { clock, severityTone, type Tone } from "../lib/format";

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
    <aside className="card" aria-label="Asset library" style={{ flex: "1 1 220px", maxWidth: 250, gap: 4 }}>
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

const SECTION: React.CSSProperties = { margin: 0, textTransform: "uppercase", letterSpacing: ".05em" };

/** One block of the asset card: a small heading and its content, laid out side by side with the others. */
function Block({ title, children }: { title: string; children: React.ReactNode }) {
  return (
    <section className="cell-stack" style={{ gap: 8, minWidth: 0, padding: 12, border: "1px solid var(--border)", borderRadius: 10 }} aria-label={title}>
      <h3 className="small faint" style={SECTION}>{title}</h3>
      {children}
    </section>
  );
}

/**
 * The selected asset, under the asset table and as wide as it: a header with state and actions,
 * four blocks side by side (location, support, monitoring, incidents), then metrics in narrow
 * tiles (six to a row on a wide screen) and capacity. Layout only; the data is the same as before.
 */
function AssetPanel({ assetId, onClose, onEdit }: { assetId: string; onClose: () => void; onEdit: (mode: EditorMode, card: AssetCard) => void }) {
  const [card, setCard] = useState<AssetCard | null>(null);
  const [error, setError] = useState("");
  const [allIncidents, setAllIncidents] = useState(false);
  useEffect(() => {
    setCard(null);
    getJson<AssetCard>(`/api/v1/inventory/${encodeURIComponent(assetId)}`).then(setCard).catch((e: Error) => setError(e.message));
  }, [assetId]);
  if (error) return <Card><div className="banner error" role="alert">{error}</div></Card>;
  if (!card) return <Card><span className="muted">Loading…</span></Card>;
  const st = STATE[card.state];
  const dl = (rows: [string, string][]) => {
    const shown = rows.filter(([, v]) => v);
    if (!shown.length) return <span className="small faint">Nothing recorded.</span>;
    return (
      <dl style={{ margin: 0, display: "grid", gridTemplateColumns: "max-content minmax(0,1fr)", gap: "6px 12px", fontSize: 13 }}>
        {shown.map(([k, v]) => (
          <div key={k} style={{ display: "contents" }}>
            <dt className="faint">{k}</dt>
            <dd style={{ margin: 0, minWidth: 0, overflowWrap: "anywhere" }}>{v}</dd>
          </div>
        ))}
      </dl>
    );
  };
  const support = card.details.filter((d) => !["site", "row", "rack", "customer"].includes(d.key)).map((d) => [d.label, d.value] as [string, string]);
  return (
    <Card
      title={card.hostname}
      actions={
        <>
          <Chip tone={st.tone}>{st.label}</Chip>
          {card.can_edit ? <button className="btn primary" type="button" onClick={() => onEdit("edit", card)}>Edit</button> : null}
          {card.can_edit ? <button className="btn" type="button" onClick={() => onEdit("clone", card)}>Clone</button> : null}
          <button className="btn" onClick={onClose} aria-label="Close asset card">Close</button>
        </>
      }
    >
      <span className="small muted">{[card.type, card.ip, card.environment].filter(Boolean).join(" · ")}</span>
      <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fit, minmax(240px, 1fr))", gap: 12, alignItems: "start" }}>
        <Block title="Location and owner">
          {dl([["Site", card.site], ["Row / rack", [card.row, card.rack].filter(Boolean).join(" / ")], ["Customer", card.customer], ["Owner", card.owner], ["Contact", [card.contact_name, card.owner_phone, card.owner_email].filter(Boolean).join(" · ")], ["Uplink / parent", card.parent]])}
          {card.notes ? <p className="small muted" style={{ margin: 0 }}>{card.notes}</p> : null}
        </Block>
        {support.length ? <Block title="Support and runbook">{dl(support)}</Block> : null}
        <Block title="Monitoring sources">
          <div className="row" style={{ gap: 6 }}>{card.sources.length ? card.sources.map((s) => <Chip key={s} tone="ok">{s}</Chip>) : <span className="muted">None: nothing watches this asset.</span>}</div>
          {dl([["Scrape", card.scrape_address], ["Profile", card.monitoring_profile], ["SNMP", card.snmp ? `${card.snmp.snmp_version}${card.snmp.snmp_version === "v3" ? ` · user ${card.snmp.snmp_v3_user}` : card.snmp.snmp_community_mode === "custom" ? " · own community" : " · default community"}${card.snmp.pending ? " · being written to snmp_exporter" : ""}` : ""], ["GLPI", card.glpi_id && `#${card.glpi_id}`], ["Zabbix", card.zabbix_hostid]])}
        </Block>
        <Block title="Recent incidents">
          {card.incidents.length === 0 ? <span className="muted">None.</span> : null}
          {(allIncidents ? card.incidents : card.incidents.slice(0, 4)).map((i) => (
            <Link key={i.number} to={`/incidents/${encodeURIComponent(i.number)}`} className="cell-stack" style={{ gap: 0, color: "var(--text)" }}>
              <span className="row" style={{ gap: 8 }}><Dot tone={severityTone(i.severity)} />{i.title}</span>
              <span className="small faint" style={{ paddingLeft: 16 }}>{clock(i.started_at)} · {i.status}</span>
            </Link>
          ))}
          {card.incidents.length > 4 ? (
            <button className="btn" type="button" style={{ alignSelf: "flex-start" }} onClick={() => setAllIncidents(!allIncidents)}>
              {allIncidents ? "Show fewer" : `Show all ${card.incidents.length}`}
            </button>
          ) : null}
        </Block>
      </div>
      <IncidentMetrics key={`m-${card.asset_id}`} assetId={card.asset_id} tileWidth={150} />
      <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fit, minmax(320px, 1fr))", gap: 12, alignItems: "start" }}>
        <section className="cell-stack" style={{ gap: 8, minWidth: 0 }} aria-label="Capacity"><CapacityCard assetId={card.asset_id} /></section>
        {card.can_edit ? <section className="cell-stack" style={{ gap: 8, minWidth: 0 }}><MonitoringCheck key={card.asset_id} assetId={card.asset_id} /></section> : null}
      </div>
    </Card>
  );
}

export function InventoryPage() {
  const [filters, setFilters] = useState<Filters>(EMPTY);
  const [query, setQuery] = useState(() => new URLSearchParams(window.location.search).get("q") ?? "");
  const [offset, setOffset] = useState(0);
  const [PAGE, setPageSize] = usePageSize("inventory");
  const [data, setData] = useState<InventoryPage | null>(null);
  const [selected, setSelected] = useState("");
  const [error, setError] = useState("");
  const [editor, setEditor] = useState<{ mode: EditorMode; card: AssetCard | null } | null>(null);
  const [options, setOptions] = useState<AssetFormOptions | null>(null);
  const [reload, setReload] = useState(0);
  const [notice, setNotice] = useState("");
  const [importing, setImporting] = useState(false);
  const [sources, setSources] = useState(false);
  const [picked, setPicked] = useState<string[]>([]);
  const [askDelete, setAskDelete] = useState(false);
  const [view, setView] = useState<"list" | "tree">(() => (new URLSearchParams(window.location.search).get("view") === "tree" ? "tree" : "list"));

  const openEditor = (mode: EditorMode, card: AssetCard | null) => {
    setNotice("");
    if (options) return setEditor({ mode, card });
    getJson<AssetFormOptions>("/api/v1/inventory-form")
      .then((o) => (setOptions(o), setEditor({ mode, card })))
      .catch((e: Error) => setError(e.message));
  };
  const closeEditor = (savedId: string | null) => {
    const mode = editor?.mode;
    setEditor(null);
    if (savedId) {
      setNotice(mode === "edit" ? "Asset saved." : "Asset added.");
      setSelected("");
      setReload((n) => n + 1);
      window.setTimeout(() => setSelected(savedId), 0);
    }
  };

  useEffect(() => {
    const params = new URLSearchParams({ ...filters, q: query, limit: String(PAGE), offset: String(offset) });
    const timer = window.setTimeout(() => {
      getJson<InventoryPage>(`/api/v1/inventory?${params}`)
        .then((next) => (setData(next), setError("")))
        .catch((e: Error) => setError(e.message));
    }, query ? 250 : 0);
    return () => window.clearTimeout(timer);
  }, [filters, query, offset, PAGE, reload]);

  const panelRef = useRef<HTMLDivElement>(null);
  // The asset card opens under the table: bring it into view when an asset is picked.
  useEffect(() => {
    if (selected) panelRef.current?.scrollIntoView?.({ behavior: "smooth", block: "start" });
  }, [selected]);

  const set = (next: Filters) => (setFilters(next), setOffset(0));
  const removePicked = () =>
    postJson<{ removed: string[]; refused: { asset_id: string; reason: string }[] }>("/api/v1/inventory/bulk-delete", { ids: picked })
      .then((r) => {
        setNotice(`Removed ${r.removed.length} asset${r.removed.length === 1 ? "" : "s"}.${r.refused.length ? ` Not removed: ${r.refused.map((x) => `${x.asset_id} (${x.reason})`).join("; ")}` : ""}`);
        setPicked([]);
        setAskDelete(false);
        setSelected("");
        setReload((n) => n + 1);
      })
      .catch((e: Error) => setError(e.message));
  return (
    <>
      <header className="page-head">
        <div>
          <h1>Inventory</h1>
          <span className="muted">One record per asset · {data ? `${data.all} assets` : "loading…"}</span>
        </div>
        <div className="row">
          <Link className="btn" to="/inventory/map">Dependency map</Link>
          <Link className="btn" to="/inventory/racks">Racks</Link>
          <button className="btn" type="button" onClick={() => setSources(!sources)} aria-expanded={sources}>Sources</button>
          {data?.can_edit ? <button className="btn" type="button" onClick={() => openEditor("new", null)}>New asset by hand</button> : null}
          {data?.can_edit ? <button className="btn" type="button" onClick={() => (setNotice(""), setImporting(true))}>Import CSV</button> : null}
          <span className="seg" role="group" aria-label="View">
            <button type="button" className={`btn${view === "list" ? " on" : ""}`} aria-pressed={view === "list"} onClick={() => setView("list")}>List</button>
            <button type="button" className={`btn${view === "tree" ? " on" : ""}`} aria-pressed={view === "tree"} onClick={() => setView("tree")}>Tree</button>
          </span>
          <a className="btn" href={`/api/v1/inventory.csv?${new URLSearchParams({ ...filters, q: query })}`}>Export CSV</a>
          {data?.can_edit ? <Link className="btn primary" to="/inventory/add">Add devices</Link> : null}
        </div>
      </header>
      {error ? <div className="banner error" role="alert">{error}</div> : null}
      {notice ? <div className="banner" role="status" style={{ background: "var(--ok-bg)" }}>{notice}</div> : null}
      {sources ? <SourcesPanel onClose={() => setSources(false)} onChanged={() => setReload((r) => r + 1)} /> : null}
      {importing ? <ImportCsv onClose={() => setImporting(false)} onImported={(n) => n && setReload((r) => r + 1)} /> : null}
      {editor && options ? (
        <AssetEditor
          key={`${editor.mode}-${editor.card?.asset_id ?? "new"}`}
          mode={editor.mode}
          card={editor.card}
          options={options}
          onDone={closeEditor}
          onDeleted={() => (setEditor(null), setSelected(""), setNotice("Asset deleted."), setReload((n) => n + 1))}
        />
      ) : null}
      <div className="cols">
        {data ? <Library data={data.library} filters={filters} set={set} /> : null}
        <div style={{ flex: "999 1 560px", minWidth: 0 }}>
          {view === "tree" ? (
            <div className="cell-stack" style={{ gap: 12 }}>
              <input className="search" type="search" aria-label="Search assets" placeholder="Hostname, IP, type…" value={query} onChange={(e) => setQuery(e.target.value)} />
              <InventoryTree query={query} selected={selected} onPick={setSelected} reload={reload} />
            </div>
          ) : (
          <Card>
            <div className="row">
              <input className="search" type="search" aria-label="Search assets" placeholder="Hostname, IP, owner, rack, customer…" value={query} onChange={(e) => (setQuery(e.target.value), setOffset(0))} />
              {Object.entries(filters).filter(([, v]) => v).map(([k, v]) => (
                <button key={k} className="btn" onClick={() => set({ ...filters, [k]: "" })} aria-label={`Remove filter ${k}`}>{v} ×</button>
              ))}
            </div>
            {data?.can_edit && picked.length ? (
              <div className="row" role="group" aria-label="Selected assets">
                <span className="small">{picked.length} selected</span>
                {askDelete ? (
                  <>
                    <span className="small" style={{ color: "var(--crit)" }}>Remove {picked.length} asset{picked.length === 1 ? "" : "s"}? Their incidents stay, without the asset.</span>
                    <button className="btn" type="button" style={{ borderColor: "var(--crit)", color: "var(--crit)" }} onClick={removePicked}>Yes, remove</button>
                    <button className="btn" type="button" onClick={() => setAskDelete(false)}>Keep</button>
                  </>
                ) : <button className="btn" type="button" onClick={() => setAskDelete(true)}>Remove selected</button>}
                <button className="btn" type="button" onClick={() => (setPicked([]), setAskDelete(false))}>Clear</button>
              </div>
            ) : null}
            <div className="table-wrap">
              <table className="data" style={{ minWidth: 880 }}>
                <thead>
                  <tr>
                    {data?.can_edit ? (
                      <th style={{ width: 36 }}>
                        <input type="checkbox" aria-label="Select all on this page" checked={Boolean(data.rows.length) && data.rows.every((r) => picked.includes(r.asset_id))}
                          onChange={(e) => setPicked(e.target.checked ? Array.from(new Set([...picked, ...data.rows.map((r) => r.asset_id)])) : picked.filter((id) => !data.rows.some((r) => r.asset_id === id)))} />
                      </th>
                    ) : null}
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
                        {data?.can_edit ? (
                          <td><input type="checkbox" aria-label={`Select ${row.hostname}`} checked={picked.includes(row.asset_id)} onChange={(e) => setPicked(e.target.checked ? [...picked, row.asset_id] : picked.filter((id) => id !== row.asset_id))} /></td>
                        ) : null}
                        <td><span className="row nowrap" style={{ gap: 8 }}><Dot tone={st.tone} /><span className="small muted">{st.label}</span></span></td>
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
            {data ? <Pager total={data.total} offset={offset} size={PAGE} onOffset={setOffset} onSize={setPageSize} label="assets" /> : null}
          </Card>
          )}
          {selected ? (
            <div ref={panelRef} style={{ marginTop: 20, scrollMarginTop: 16 }}>
              <AssetPanel key={`${selected}-${reload}`} assetId={selected} onClose={() => setSelected("")} onEdit={openEditor} />
            </div>
          ) : null}
        </div>
      </div>
    </>
  );
}
