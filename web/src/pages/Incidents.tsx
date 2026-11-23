import { useEffect, useState } from "react";
import { NavLink } from "react-router-dom";
import { IncidentTable } from "../components/IncidentTable";
import { useRoleRank } from "../lib/me";
import { Pager, usePageSize } from "../components/Pager";
import { Card } from "../components/ui";
import { SavedViews, type ViewFilters } from "../components/SavedViews";
import { getJson, type IncidentPage } from "../lib/api";
import { severityLabel } from "../lib/format";

type Tab = "active" | "acknowledged" | "resolved" | "all";
const TABS: { key: Tab; label: string }[] = [
  { key: "active", label: "Not acknowledged" },
  { key: "acknowledged", label: "Acknowledged" },
  { key: "resolved", label: "Resolved" },
  { key: "all", label: "All" },
];

/** Sub-navigation shared by the Incidents pages (design: Incidents | On-call). */
export function IncidentsNav() {
  return (
    <nav className="tabs" aria-label="Incidents sections">
      <NavLink end to="/incidents" className="tab" style={({ isActive }) => (isActive ? { color: "var(--text)", borderBottomColor: "var(--accent)" } : undefined)}>
        Incidents
      </NavLink>
      <NavLink to="/incidents/on-call" className="tab" style={({ isActive }) => (isActive ? { color: "var(--text)", borderBottomColor: "var(--accent)" } : undefined)}>
        On-call
      </NavLink>
    </nav>
  );
}

export function IncidentsPage() {
  const [tab, setTab] = useState<Tab>("active");
  const [severity, setSeverity] = useState("");
  const [query, setQuery] = useState("");
  const [from, setFrom] = useState("");
  const [to, setTo] = useState("");
  const [team, setTeam] = useState("");
  const [offset, setOffset] = useState(0);
  const [PAGE, setPageSize] = usePageSize("incidents");
  const [data, setData] = useState<IncidentPage | null>(null);
  const [error, setError] = useState("");
  const [reload, setReload] = useState(0);
  const canWork = useRoleRank() >= 1;

  useEffect(() => {
    const params = new URLSearchParams({ tab, severity, q: query, date_from: from, date_to: to, team, limit: String(PAGE), offset: String(offset) });
    const timer = window.setTimeout(() => {
      getJson<IncidentPage>(`/api/v1/incidents-page?${params}`)
        .then((next) => (setData(next), setError("")))
        .catch((err: Error) => setError(err.message));
    }, query ? 250 : 0); // debounce typing
    return () => window.clearTimeout(timer);
  }, [tab, severity, query, from, to, team, offset, PAGE, reload]);
  const csv = `/api/v1/incidents-page.csv?${new URLSearchParams({ tab, severity, q: query, date_from: from, date_to: to, team })}`;
  const current: ViewFilters = { tab, severity, q: query, team, date_from: from, date_to: to };
  const apply = (f: ViewFilters) => pick(() => {
    setTab((["active", "acknowledged", "resolved", "all"].includes(f.tab) ? f.tab : "active") as Tab);
    setSeverity(f.severity || "");
    setQuery(f.q || "");
    setTeam(f.team || "");
    setFrom(f.date_from || "");
    setTo(f.date_to || "");
  });
  const teams = Array.isArray(data?.teams) ? data!.teams : [];

  const pick = (next: () => void) => {
    next();
    setOffset(0);
  };

  return (
    <>
      <header className="page-head">
        <div>
          <h1>Incidents</h1>
          <span className="muted">Every incident, newest first</span>
        </div>
      </header>
      <IncidentsNav />
      {error ? <div className="banner error" role="alert">{error}</div> : null}
      <Card>
        <div className="tabs" role="tablist" aria-label="Incident status">
          {TABS.map((t) => (
            <button key={t.key} role="tab" className="tab" aria-selected={tab === t.key} onClick={() => pick(() => setTab(t.key))}>
              {t.label} {data ? data.counts[t.key] : ""}
            </button>
          ))}
        </div>
        <div className="row">
          {["", "CRITICAL", "HIGH", "WARNING"].map((s) => (
            <button key={s || "all"} className="btn" aria-pressed={severity === s} onClick={() => pick(() => setSeverity(s))}>
              {s ? severityLabel(s) : "All severities"}
            </button>
          ))}
          <input className="search" type="search" aria-label="Search incidents" placeholder="Number, title, host, IP…" value={query} onChange={(e) => pick(() => setQuery(e.target.value))} />
          {teams.length ? (
            <select className="search" aria-label="Team" value={team} onChange={(e) => pick(() => setTeam(e.target.value))} style={{ width: 180 }}>
              <option value="">All teams</option>
              {teams.map((t) => <option key={t} value={t}>{t}</option>)}
            </select>
          ) : null}
        </div>
        <SavedViews page="incidents" current={current} teams={teams} onApply={apply} />
        <div className="row small">
          <label className="row" style={{ gap: 6 }}>From <input className="search" type="date" aria-label="From day" value={from} onChange={(e) => pick(() => setFrom(e.target.value))} /></label>
          <label className="row" style={{ gap: 6 }}>To <input className="search" type="date" aria-label="To day" value={to} onChange={(e) => pick(() => setTo(e.target.value))} /></label>
          {from || to ? <button className="btn" type="button" onClick={() => pick(() => (setFrom(""), setTo("")))}>Any day</button> : null}
          <a className="btn" href={csv} download>Export CSV</a>
        </div>
        <IncidentTable rows={data?.rows ?? []} loaded={Boolean(data)} canWork={canWork} onChanged={() => setReload((n) => n + 1)} />
        {data ? <Pager total={data.total} offset={offset} size={PAGE} onOffset={setOffset} onSize={setPageSize} label="incidents" /> : null}
      </Card>
    </>
  );
}
