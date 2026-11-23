import { createContext, useContext, useEffect, useMemo, useState } from "react";
import { Link } from "react-router-dom";
import { ShiftSummary } from "../components/ShiftSummary";
import { Bar, Card, Stat } from "../components/ui";
import { getJson, type Noc as NocData } from "../lib/api";
import { clock, minutesLabel, severityLabel } from "../lib/format";
import { IncidentTable } from "../components/IncidentTable";
import { usePaged } from "../components/Pager";

const REFRESH_MS = 10_000;
type TabKey = "active" | "acknowledged" | "resolved_today";
const TABS: { key: TabKey; label: string }[] = [
  { key: "active", label: "Not acknowledged" },
  { key: "acknowledged", label: "Acknowledged" },
  { key: "resolved_today", label: "Resolved today" },
];

/** Polls /api/v1/noc; keeps showing the last good data when one poll fails. Call once (App). */
export function useNocPoll() {
  const [data, setData] = useState<NocData | null>(null);
  const [error, setError] = useState<string>("");
  useEffect(() => {
    let alive = true;
    const load = () =>
      getJson<NocData>("/api/v1/noc")
        .then((next) => alive && (setData(next), setError("")))
        .catch((err: Error) => alive && setError(err.message));
    load();
    const timer = window.setInterval(load, REFRESH_MS);
    return () => {
      alive = false;
      window.clearInterval(timer);
    };
  }, []);
  return { data, error };
}

export const NocContext = createContext<{ data: NocData | null; error: string }>({ data: null, error: "" });
export const useNoc = () => useContext(NocContext);

export function NocPage() {
  const { data, error } = useNoc();
  const [tab, setTab] = useState<TabKey>("active");
  const [query, setQuery] = useState("");
  const [severity, setSeverity] = useState<string>("");

  const rows = useMemo(() => {
    const list = data?.incidents[tab] ?? [];
    const q = query.trim().toLowerCase();
    return list.filter(
      (row) =>
        (!severity || row.severity === severity) &&
        (!q || [row.number, row.title, row.asset?.hostname, row.asset?.ip, row.asset?.type].some((v) => v?.toLowerCase().includes(q))),
    );
  }, [data, tab, query, severity]);

  const c = data?.counts;
  const nocPage = usePaged(rows, "noc", "incidents");
  return (
    <>
      <header className="page-head">
        <div className="cell-stack" style={{ gap: 6 }}>
          <span className="row" style={{ gap: 8 }}>
            <span className="label">NOC</span>
            {c ? (
              <span className="label row" style={{ gap: 6, color: error ? "var(--crit)" : "var(--ok)" }}>
                <span aria-hidden="true" className={error ? "dot" : "dot pulse"} style={{ background: "currentColor", width: 7, height: 7 }} />
                {error ? "Stale" : "Live"}
              </span>
            ) : null}
          </span>
          <h1>NOC overview</h1>
          <span className="muted">
            {c ? `${c.hosts_total} assets · updated ${clock(data!.generated_at)}` : "Loading…"}
          </span>
        </div>
        <a className="btn" href="/app/tv">Wall screen (TV mode)</a>
      </header>
      {error ? <div className="banner error" role="alert">Could not refresh: {error}. Showing the last data.</div> : null}
      {c ? (
        <div className="strip" role="group" aria-label="Key numbers">
          <Stat label="Open incidents" value={String(c.open)} detail={`${c.critical} critical · ${c.high} high · ${c.warning} warning`} tone={c.critical ? "crit" : undefined} />
          <Stat label="Not acknowledged" value={String(c.not_acked)} tone={c.not_acked ? "high" : undefined} />
          <Stat label="Hosts down" value={`${c.hosts_down} / ${c.hosts_total}`} detail={c.hosts_down_names.join(", ")} />
          <Stat label="MTTA today" value={minutesLabel(c.mtta_today_min)} />
          <Stat label="MTTR 7 days" value={minutesLabel(c.mttr_7d_min)} />
        </div>
      ) : null}
      <Card>
        <div className="tabs" role="tablist" aria-label="Incident lists">
          {TABS.map((t) => (
            <button key={t.key} role="tab" className="tab" aria-selected={tab === t.key} onClick={() => setTab(t.key)}>
              {t.label} {data ? data.incidents[t.key].length : ""}
            </button>
          ))}
        </div>
        <div className="row">
          {["", "CRITICAL", "HIGH", "WARNING"].map((s) => (
            <button key={s || "all"} className="btn" aria-pressed={severity === s} onClick={() => setSeverity(s)}>
              {s ? severityLabel(s) : "All severities"}
            </button>
          ))}
          <input className="search" type="search" aria-label="Filter incidents" placeholder="Filter by host, IP, incident…" value={query} onChange={(e) => setQuery(e.target.value)} />
        </div>
        <IncidentTable rows={nocPage.page} loaded={Boolean(data)} />
        {nocPage.pager}
      </Card>
      {data ? (
        <div className="cols">
          <Card title="Health by group">
            {data.health.length === 0 ? <span className="muted">No assets yet.</span> : null}
            {data.health.map((g) => (
              <div key={g.group} className="cell-stack" style={{ gap: 6 }}>
                <div className="row" style={{ justifyContent: "space-between" }}>
                  <span>{g.group}</span>
                  <span className="small muted">{g.up} / {g.total} up</span>
                </div>
                <Bar percent={g.total ? (g.up / g.total) * 100 : 100} tone={g.up === g.total ? "ok" : "warn"} />
              </div>
            ))}
          </Card>
          <Card title="On call now" actions={<Link className="small" to="/incidents/on-call">Schedule</Link>}>
            {data.oncall ? (
              <div className="cell-stack">
                <span style={{ fontWeight: 600 }}>{data.oncall.name}</span>
                <span className="small muted">{[data.oncall.phone, data.oncall.email].filter(Boolean).join(" · ")}</span>
              </div>
            ) : (
              <span className="muted">Nobody on the roster for this hour.</span>
            )}
          </Card>
        </div>
      ) : null}
      {data && data.ai.forgeai !== "off" ? <ShiftSummary /> : null}
    </>
  );
}
