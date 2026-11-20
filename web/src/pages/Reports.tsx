import { useEffect, useState } from "react";
import { OutboxCard } from "../components/ActivityPanels";
import { ReportSchedules } from "../components/ReportSchedules";
import { SloCard } from "../components/SloCard";
import { Card } from "../components/ui";
import { DailyBars } from "../components/DailyBars";
import { getJson, type PeriodStats, type Reports } from "../lib/api";
import { clock, minutesLabel } from "../lib/format";

/** "−12% vs previous" style delta; lower is better for every figure on this page. */
function delta(now: number | null, before: number | null, unit: "%" | "min") {
  if (now === null || before === null || before === 0) return { text: "no earlier data", tone: "var(--faint)" };
  const diff = unit === "%" ? Math.round(((now - before) / before) * 100) : Math.round(now - before);
  const sign = diff > 0 ? "+" : diff < 0 ? "−" : "±";
  return { text: `${sign}${Math.abs(diff)}${unit === "%" ? "%" : " min"} vs previous`, tone: diff > 0 ? "var(--high)" : "var(--ok)" };
}

function Kpi({ label, value, d }: { label: string; value: string; d: { text: string; tone: string } }) {
  return (
    <div className="stat">
      <span className="stat-label">{label}</span>
      <span className="stat-value">{value}</span>
      <span className="small" style={{ color: d.tone }}>{d.text}</span>
    </div>
  );
}

function Table({ head, rows }: { head: string[]; rows: (string | number)[][] }) {
  return (
    <div className="table-wrap">
      <table className="data">
        <thead><tr>{head.map((h) => <th key={h}>{h}</th>)}</tr></thead>
        <tbody>{rows.map((r, i) => <tr key={i}>{r.map((c, j) => <td key={j} style={j === 0 ? { fontWeight: 600 } : undefined}>{c}</td>)}</tr>)}</tbody>
      </table>
      {rows.length === 0 ? <div className="empty">Nothing in this period.</div> : null}
    </div>
  );
}

export function ReportsPage() {
  const [days, setDays] = useState(30);
  const [data, setData] = useState<Reports | null>(null);
  const [error, setError] = useState("");
  const [tick, setTick] = useState(0);
  const reload = () => setTick((n) => n + 1);
  useEffect(() => {
    getJson<Reports>(`/api/v1/reports-page?days=${days}`).then((d) => (setData(d), setError(""))).catch((e: Error) => setError(e.message));
  }, [days, tick]);
  const c: PeriodStats | undefined = data?.current;
  const p: PeriodStats | undefined = data?.previous;
  return (
    <>
      <header className="page-head">
        <div>
          <h1>Reports</h1>
          <span className="muted">{data ? `Last ${data.days} days · ${clock(data.from)} – ${clock(data.to)} · compared with the ${data.days} days before` : "Loading…"}</span>
        </div>
        <div className="row">
          {[7, 30, 90].map((d) => (
            <button key={d} className="btn" aria-pressed={days === d} onClick={() => setDays(d)}>Last {d} days</button>
          ))}
          <a className="btn" href={`/api/v1/reports/incidents.csv?days=${days}`}>Export CSV</a>
        </div>
      </header>
      {error ? <div className="banner error" role="alert">{error}</div> : null}
      {c && p ? (
        <div className="strip" role="group" aria-label="Period summary">
          <Kpi label="Incidents" value={String(c.incidents)} d={delta(c.incidents, p.incidents, "%")} />
          <Kpi label="Critical" value={String(c.critical)} d={delta(c.critical, p.critical, "%")} />
          <Kpi label="MTTA" value={minutesLabel(c.mtta_min)} d={delta(c.mtta_min, p.mtta_min, "min")} />
          <Kpi label="MTTR" value={minutesLabel(c.mttr_min)} d={delta(c.mttr_min, p.mttr_min, "min")} />
          <div className="stat">
            <span className="stat-label">Causes confirmed</span>
            <span className="stat-value">{c.reviewed} / {c.incidents}</span>
            <span className="small faint">reviewed by analysts</span>
          </div>
        </div>
      ) : null}
      <SloCard />
      {data ? (
        <div className="cols">
          <div style={{ flex: "999 1 640px", minWidth: 0, display: "flex", flexDirection: "column", gap: 20 }}>
            <Card title="Incidents per day">
              <DailyBars daily={data.daily} />
            </Card>
            <Card title="By asset type">
              <Table head={["Type", "Incidents", "Critical", "MTTA", "MTTR"]} rows={data.by_type.map((t) => [t.group, t.incidents, t.critical, minutesLabel(t.mtta_min), minutesLabel(t.mttr_min)])} />
            </Card>
            <div className="cols">
              <Card title="Noisiest assets">
                <Table head={["Asset", "Incidents"]} rows={data.noisy_assets.map((a) => [a.asset, a.incidents])} />
              </Card>
              <Card title="Most frequent alerts">
                <Table head={["Alert", "Incidents"]} rows={data.top_alerts.map((a) => [a.alert, a.incidents])} />
              </Card>
            </div>
            <Card title="Host-down time" actions={<span className="small faint">Summed host-down incidents, not a measured SLA</span>}>
              <Table head={["Asset", "Minutes down"]} rows={data.host_down.map((a) => [a.asset, a.minutes])} />
            </Card>
          </div>
          <div style={{ flex: "1 1 340px", minWidth: 0 }}>
            <ReportSchedules rows={data.scheduled} canEdit={data.can_edit} onChanged={reload} />
            <OutboxCard />
          </div>
        </div>
      ) : null}
    </>
  );
}
