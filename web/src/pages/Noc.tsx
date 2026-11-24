import { createContext, useContext, useEffect, useMemo, useState } from "react";
import { Card, Stat } from "../components/ui";
import { getJson, type Noc as NocData } from "../lib/api";
import { clock, minutesLabel, severityLabel } from "../lib/format";
import { IncidentTable } from "../components/IncidentTable";
import { usePaged } from "../components/Pager";
import { useRoleRank } from "../lib/me";
import { MajorBanner, ShiftLine } from "../components/Handover";
import { PowerPanel } from "../components/PowerPanel";
import { NetworkPanel } from "../components/NetworkPanel";

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
    window.addEventListener("forgesre:noc-refresh", load);
    return () => {
      alive = false;
      window.clearInterval(timer);
      window.removeEventListener("forgesre:noc-refresh", load);
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
  const canWork = useRoleRank() >= 1;
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
      <MajorBanner majors={data?.major} />
      {c ? (
        <div className="strip" role="group" aria-label="Key numbers">
          <Stat label="Open incidents" value={String(c.open)} detail={`${c.critical} critical · ${c.high} high · ${c.warning} warning`} tone={c.critical ? "crit" : undefined} />
          <Stat label="Not acknowledged" value={String(c.not_acked)} tone={c.not_acked ? "high" : undefined} />
          <Stat label="Hosts down" value={`${c.hosts_down} / ${c.hosts_total}`} detail={c.hosts_down_names.join(", ")} />
          <Stat label="MTTA today" value={minutesLabel(c.mtta_today_min)} />
          <Stat label="MTTR 7 days" value={minutesLabel(c.mttr_7d_min)} />
        </div>
      ) : null}
      {data ? <ShiftLine oncall={data.oncall} handover={data.handover} /> : null}
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
        <IncidentTable rows={nocPage.page} loaded={Boolean(data)} canWork={canWork} onChanged={() => window.dispatchEvent(new Event("forgesre:noc-refresh"))} />
        {nocPage.pager}
      </Card>
      <PowerPanel />
      <NetworkPanel />
    </>
  );
}
