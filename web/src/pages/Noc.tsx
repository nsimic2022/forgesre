import { createContext, useContext, useEffect, useMemo, useState } from "react";
import { Bar, Card, Chip, Dot, Stat } from "../components/ui";
import { getJson, type Noc as NocData, type NocIncident } from "../lib/api";
import { FORGEAI_LABEL, clock, duration, minutesLabel, severityLabel, severityTone } from "../lib/format";

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

function IncidentRow({ row }: { row: NocIncident }) {
  const tone = severityTone(row.severity);
  const ai = FORGEAI_LABEL[row.forgeai] ?? FORGEAI_LABEL.none;
  return (
    <tr>
      <td>
        <span className="row" style={{ gap: 8 }}>
          <Dot tone={tone} />
          <span style={{ color: `var(--${tone === "info" ? "accent" : tone})`, fontWeight: 600, fontSize: 12.5 }}>{severityLabel(row.severity)}</span>
        </span>
      </td>
      <td>
        <div className="cell-stack">
          <span className="mono">{clock(row.started_at)}</span>
          <span className="small muted">{duration(row.started_at, row.resolved_at ? new Date(row.resolved_at) : new Date())}</span>
        </div>
      </td>
      <td>
        <div className="cell-stack">
          <span style={{ fontWeight: 600 }}>{row.asset?.hostname ?? "—"}</span>
          <span className="small muted">{row.asset?.type ?? ""}</span>
        </div>
      </td>
      <td>
        <div className="cell-stack">
          <a href={`/incidents/${encodeURIComponent(row.number)}`} style={{ color: "var(--text)", fontWeight: 600 }}>
            {row.title} <span className="mono faint">{row.number}</span>
          </a>
          {row.detail ? <span className="small muted">{row.detail}</span> : null}
        </div>
      </td>
      <td className="small muted">{row.source || "—"}</td>
      <td>
        {row.ack_at ? (
          <div className="cell-stack">
            <span className="small" style={{ color: "var(--ok)", fontWeight: 600 }}>Acked</span>
            <span className="small muted">{row.ack_by}</span>
          </div>
        ) : row.resolved_at ? (
          <span className="small muted">—</span>
        ) : (
          <span className="small" style={{ color: `var(--${tone === "crit" ? "crit" : "high"})`, fontWeight: 600 }}>Not acked</span>
        )}
      </td>
      <td><Chip tone="off">{row.level}</Chip></td>
      <td className="small muted">{row.messages} sent</td>
      <td className="small muted">{row.forgerca || "—"}</td>
      <td><Chip tone={ai.tone}>{ai.text}</Chip></td>
    </tr>
  );
}

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
  return (
    <>
      <header className="page-head">
        <div>
          <h1>NOC overview</h1>
          <span className="muted">
            {c ? `${c.hosts_total} assets · updated ${clock(data!.generated_at)}` : "Loading…"}
          </span>
        </div>
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
        <div className="table-wrap">
          <table className="data" style={{ minWidth: 1180 }}>
            <thead>
              <tr>
                <th style={{ width: 100 }}>Severity</th>
                <th style={{ width: 90 }}>Started</th>
                <th style={{ width: 150 }}>Host</th>
                <th>Problem</th>
                <th style={{ width: 90 }}>Source</th>
                <th style={{ width: 96 }}>Ack</th>
                <th style={{ width: 70 }}>Level</th>
                <th style={{ width: 80 }}>Messages</th>
                <th style={{ width: 170 }}>ForgeRCA</th>
                <th style={{ width: 128 }}>ForgeAI</th>
              </tr>
            </thead>
            <tbody>
              {rows.map((row) => (
                <IncidentRow key={row.number} row={row} />
              ))}
            </tbody>
          </table>
          {data && rows.length === 0 ? <div className="empty">Nothing here.</div> : null}
        </div>
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
          <Card title="On call now" actions={<a className="small" href="/escalation">Schedule</a>}>
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
    </>
  );
}
