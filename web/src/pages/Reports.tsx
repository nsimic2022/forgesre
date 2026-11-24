import { useEffect, useState } from "react";
import { OutboxCard } from "../components/ActivityPanels";
import { ReportSchedules } from "../components/ReportSchedules";
import { SloCard } from "../components/SloCard";
import { AlertQuality } from "../components/AlertQuality";
import { OpenActionsCard, SlaCard } from "../components/IncidentProcess";
import { LifecycleCard } from "../components/Racks";
import { Card } from "../components/ui";
import { DailyBars, type Day } from "../components/DailyBars";
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

/** One period for the whole page: the buttons at the top drive the numbers, the chart and the CSV. */
const PERIODS = [
  { id: "today", label: "Today", days: 0, trend: "today", chart: "Incidents per hour, today", since: "Today since midnight", before: "yesterday" },
  { id: "7d", label: "Last 7 days", days: 7, trend: "7d", chart: "Incidents per day, last 7 days", since: "Last 7 days", before: "the 7 days before" },
  { id: "30d", label: "Last 30 days", days: 30, trend: "30d", chart: "Incidents per day, last 30 days", since: "Last 30 days", before: "the 30 days before" },
  { id: "90d", label: "Last 90 days", days: 90, trend: "90d", chart: "Incidents per week, last 90 days", since: "Last 90 days", before: "the 90 days before" },
] as const;
type Period = (typeof PERIODS)[number];

const SECTIONS = [
  { id: "summary", label: "Summary" },
  { id: "sources", label: "Where incidents come from" },
  { id: "service", label: "Service levels" },
  { id: "follow", label: "Follow-up" },
  { id: "delivery", label: "Sending reports" },
];

function Section({ id, title, note, children }: { id: string; title: string; note?: string; children: React.ReactNode }) {
  return (
    <section id={`r-${id}`} className="report-section" aria-labelledby={`r-${id}-h`}>
      <div className="report-section-head">
        <h2 id={`r-${id}-h`}>{title}</h2>
        {note ? <span className="small muted">{note}</span> : null}
      </div>
      {children}
    </section>
  );
}

/**
 * Reports, in five sections under one period picker: summary (numbers and chart), where incidents
 * come from, service levels (SLA and SLO), follow-up, and sending reports. Today, 7, 30 or 90 days
 * apply to everything that depends on a period, and to Export CSV.
 */
export function ReportsPage() {
  const [period, setPeriod] = useState<Period>(PERIODS[2]);
  const [data, setData] = useState<Reports | null>(null);
  const [error, setError] = useState("");
  const [tick, setTick] = useState(0);
  const reload = () => setTick((n) => n + 1);
  useEffect(() => {
    getJson<Reports>(`/api/v1/reports-page?days=${period.days}`).then((d) => (setData(d), setError(""))).catch((e: Error) => setError(e.message));
  }, [period, tick]);
  const c: PeriodStats | undefined = data?.current;
  const p: PeriodStats | undefined = data?.previous;
  const atLeastDay = Math.max(1, period.days);
  return (
    <>
      <header className="page-head">
        <div>
          <h1>Reports</h1>
          <span className="muted">{data ? `${period.since} · ${clock(data.from)} – ${clock(data.to)} · compared with ${period.before}` : "Loading…"}</span>
        </div>
        <div className="row" role="group" aria-label="Report period">
          {PERIODS.map((x) => (
            <button key={x.id} className="btn" aria-pressed={period.id === x.id} onClick={() => setPeriod(x)}>{x.label}</button>
          ))}
          <a className="btn" href={`/api/v1/reports/incidents.csv?days=${period.days}`}>Export CSV</a>
        </div>
      </header>
      <nav className="report-nav" aria-label="Report sections">
        {SECTIONS.map((s) => <a key={s.id} href={`#r-${s.id}`}>{s.label}</a>)}
      </nav>
      {error ? <div className="banner error" role="alert">{error}</div> : null}
      <Section id="summary" title="Summary" note={`${period.since}, compared with ${period.before}. Lower is better for every number.`}>
        {c && p ? (
          <div className="strip" role="group" aria-label="Period summary">
            <Kpi label="Incidents" value={String(c.incidents)} d={delta(c.incidents, p.incidents, "%")} />
            <Kpi label="Critical" value={String(c.critical)} d={delta(c.critical, p.critical, "%")} />
            <Kpi label="MTTA · time to acknowledge" value={minutesLabel(c.mtta_min)} d={delta(c.mtta_min, p.mtta_min, "min")} />
            <Kpi label="MTTR · time to resolve" value={minutesLabel(c.mttr_min)} d={delta(c.mttr_min, p.mttr_min, "min")} />
            <div className="stat">
              <span className="stat-label">Causes confirmed</span>
              <span className="stat-value">{c.reviewed} / {c.incidents}</span>
              <span className="small faint">reviewed by analysts</span>
            </div>
          </div>
        ) : null}
        <IncidentTrend range={period.trend} title={period.chart} />
      </Section>
      {data ? (
        <>
          <Section id="sources" title="Where incidents come from" note="What makes the most noise in this period: fix or tune these first.">
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
            <AlertQuality days={atLeastDay} />
            <Card title="Host-down time" actions={<span className="small faint">Summed host-down incidents, not a measured SLA</span>}>
              <Table head={["Asset", "Minutes down"]} rows={data.host_down.map((a) => [a.asset, a.minutes])} />
            </Card>
          </Section>
          <Section id="service" title="Service levels" note="How fast the team reacts (SLA) and how available the services are (SLO).">
            <div className="explain-pair">
              <div><strong>SLA · how fast we react</strong><span className="small muted">A promise per severity: acknowledge a critical within 15 minutes, resolve it within 4 hours. It measures the team. Targets: Settings › SLA.</span></div>
              <div><strong>SLO · how available a service is</strong><span className="small muted">A target for a business service, e.g. 99.9 % available over 30 days. The error budget is the downtime still allowed. It measures the service, not the team.</span></div>
            </div>
            <SlaCard days={atLeastDay} />
            <SloCard />
          </Section>
          <Section id="follow" title="Follow-up" note="What must still change after incidents, and hardware or licences running out.">
            <OpenActionsCard />
            <LifecycleCard />
          </Section>
          <Section id="delivery" title="Sending reports" note="Reports that go out by mail on a schedule, and every message ForgeSRE sent.">
            <ReportSchedules rows={data.scheduled} canEdit={data.can_edit} onChanged={reload} />
            <OutboxCard />
          </Section>
        </>
      ) : null}
    </>
  );
}

type Trend = { range: string; unit: string; buckets: Day[]; total: number };

/** The incidents chart for the page's period: per hour today, per day for 7 or 30 days, per week for 90. */
export function IncidentTrend({ range, title }: { range: string; title: string }) {
  const [trend, setTrend] = useState<Trend | null>(null);
  const [error, setError] = useState("");
  useEffect(() => {
    let alive = true;
    setError("");
    getJson<Trend>(`/api/v1/reports/incident-trend?range=${range}`).then((d) => alive && setTrend(d)).catch((e: Error) => alive && setError(e.message));
    return () => { alive = false; };
  }, [range]);
  return (
    <Card title={title}>
      {error ? <div className="banner error" role="alert">{error}</div> : null}
      {trend && trend.range === range
        ? <><DailyBars daily={trend.buckets} axis unit={trend.unit} /><span className="small faint">{trend.total} incidents</span></>
        : <span className="muted">Loading…</span>}
    </Card>
  );
}
