import { useEffect, useState } from "react";
import { NavLink } from "react-router-dom";
import { IncidentTable } from "../components/IncidentTable";
import { Card } from "../components/ui";
import { getJson, type IncidentPage } from "../lib/api";
import { severityLabel } from "../lib/format";

type Tab = "active" | "acknowledged" | "resolved" | "all";
const TABS: { key: Tab; label: string }[] = [
  { key: "active", label: "Not acknowledged" },
  { key: "acknowledged", label: "Acknowledged" },
  { key: "resolved", label: "Resolved" },
  { key: "all", label: "All" },
];
const PAGE = 50;

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
  const [offset, setOffset] = useState(0);
  const [data, setData] = useState<IncidentPage | null>(null);
  const [error, setError] = useState("");

  useEffect(() => {
    const params = new URLSearchParams({ tab, severity, q: query, limit: String(PAGE), offset: String(offset) });
    const timer = window.setTimeout(() => {
      getJson<IncidentPage>(`/api/v1/incidents-page?${params}`)
        .then((next) => (setData(next), setError("")))
        .catch((err: Error) => setError(err.message));
    }, query ? 250 : 0); // debounce typing
    return () => window.clearTimeout(timer);
  }, [tab, severity, query, offset]);

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
        </div>
        <IncidentTable rows={data?.rows ?? []} loaded={Boolean(data)} />
        {data && data.total > PAGE ? (
          <div className="row" style={{ justifyContent: "space-between" }}>
            <span className="small faint">
              {offset + 1}–{Math.min(offset + PAGE, data.total)} of {data.total}
            </span>
            <div className="row">
              <button className="btn" disabled={offset === 0} onClick={() => setOffset(Math.max(0, offset - PAGE))}>Previous</button>
              <button className="btn" disabled={offset + PAGE >= data.total} onClick={() => setOffset(offset + PAGE)}>Next</button>
            </div>
          </div>
        ) : null}
      </Card>
    </>
  );
}
