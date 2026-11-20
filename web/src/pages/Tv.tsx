import { useEffect, useState } from "react";
import { DailyBars } from "../components/DailyBars";
import { IncidentClock } from "../components/IncidentClock";
import { DayCard, HourlyChart, Tile } from "../components/TvWeather";
import { getJson, type NocIncident, type TvData } from "../lib/api";
import { shortNumber, tierLabel } from "../lib/format";

const REFRESH_MS = 30_000;
const STALE_MS = 90_000;
const ROTATE_MS = 30_000;
const SEV_COLOR: Record<string, string> = { CRITICAL: "var(--crit)", HIGH: "var(--high)", WARNING: "var(--warn)" };
const TABS = [["overview", "Overview"], ["burning", "Burning now"], ["clock", "12-hour clock"], ["hosts", "Hosts"]] as const;
type Tab = (typeof TABS)[number][0];

function age(iso: string | null, now: number) {
  if (!iso) return "";
  const minutes = Math.max(0, Math.round((now - new Date(iso).getTime()) / 60_000));
  return minutes < 60 ? `${minutes} min` : `${Math.floor(minutes / 60)} h ${minutes % 60} min`;
}

function Burning({ list, now }: { list: NocIncident[]; now: number }) {
  if (list.length === 0) return <span className="muted" style={{ fontSize: 24 }}>Nothing open. Quiet.</span>;
  return (
    <div className="tvw-burning">
      {list.map((i) => (
        <div key={i.number} className="tvw-panel cell-stack" style={{ gap: 4 }}>
          <span className="row" style={{ gap: 10 }}>
            <span className="mono" style={{ color: SEV_COLOR[i.severity] ?? "var(--muted)", fontWeight: 700 }}>{i.severity}</span>
            <span className="mono faint">{age(i.started_at, now)}</span>
            {i.ack_by ? <span className="faint">· {i.ack_by}</span> : <span style={{ color: "var(--crit)" }}>· not acked</span>}
          </span>
          <span style={{ fontWeight: 600, fontSize: 22 }}>{i.title}</span>
          <span className="muted small">{[i.asset?.hostname, shortNumber(i.number), tierLabel(i.level)].filter(Boolean).join(" · ")}</span>
        </div>
      ))}
    </div>
  );
}

/**
 * Wall screen at /app/tv in the style of a weather report: tabs, seven day cards, the 24-hour
 * curve and small tiles you can click for detail. "Rotate" walks the tabs every 30 s for a wall;
 * on a monitor you pick a tab. Refresh every 30 s; a Viewer account is enough. When updates stop,
 * a red banner says so, so an old picture never passes for a calm night.
 */
export function TvPage() {
  const [data, setData] = useState<TvData | null>(null);
  const [lastOk, setLastOk] = useState<number>(0);
  const [now, setNow] = useState(() => Date.now());
  const [tab, setTab] = useState<Tab>("overview");
  const [rotate, setRotate] = useState(() => new URLSearchParams(window.location.search).get("rotate") === "1");
  const [openTile, setOpenTile] = useState("");
  useEffect(() => {
    let alive = true;
    const load = () => getJson<TvData>("/api/v1/tv").then((d) => alive && (setData(d), setLastOk(Date.now()))).catch(() => undefined);
    load();
    const poll = window.setInterval(load, REFRESH_MS);
    const tick = window.setInterval(() => setNow(Date.now()), 10_000);
    return () => {
      alive = false;
      window.clearInterval(poll);
      window.clearInterval(tick);
    };
  }, []);
  useEffect(() => {
    if (!rotate) return;
    const t = window.setInterval(() => setTab((cur) => TABS[(TABS.findIndex(([k]) => k === cur) + 1) % TABS.length][0]), ROTATE_MS);
    return () => window.clearInterval(t);
  }, [rotate]);
  const stale = lastOk > 0 && now - lastOk > STALE_MS;
  const c = data?.counts;
  const burning: NocIncident[] = data ? [...data.incidents.active, ...data.incidents.acknowledged].sort((a, b) => (a.severity === b.severity ? 0 : a.severity === "CRITICAL" ? -1 : b.severity === "CRITICAL" ? 1 : 0)).slice(0, 9) : [];
  return (
    <main className="tv tvw">
      <header className="row" style={{ justifyContent: "space-between" }}>
        <div className="row" style={{ gap: 14 }}>
          <strong style={{ fontSize: 26 }}>ForgeSRE</strong>
          <span className="tv-label">NOC · wall screen</span>
          {c ? (
            <span className="row" style={{ gap: 14, marginLeft: 18 }}>
              <span className="tvw-pill">{c.open} open</span>
              <span className="tvw-pill" style={{ color: c.not_acked ? "var(--crit)" : undefined }}>{c.not_acked} not acknowledged</span>
              <span className="tvw-pill" style={{ color: c.hosts_down ? "var(--crit)" : "var(--ok)" }}>{c.hosts_down} hosts down</span>
              <span className="tvw-pill">On call: {data?.oncall?.name ?? "nobody"}{data?.oncall?.phone ? ` · ${data.oncall.phone}` : ""}</span>
            </span>
          ) : null}
        </div>
        <span className="row" style={{ gap: 8 }}>
          <span aria-hidden="true" style={{ width: 10, height: 10, borderRadius: 5, background: stale ? "var(--crit)" : "var(--ok)" }} />
          <span className="tv-label" style={{ color: stale ? "var(--crit)" : "var(--ok)" }}>{stale ? "No updates" : "Live"}</span>
          <a className="small faint" href="/app/" style={{ marginLeft: 18 }}>Exit</a>
        </span>
      </header>
      {stale ? (
        <div className="banner error" role="alert" style={{ fontSize: 20 }}>
          No update from ForgeSRE since {new Date(lastOk).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" })}. What you see may be old.
        </div>
      ) : null}
      <nav className="row tvw-tabs" role="tablist" aria-label="Wall screen views">
        {TABS.map(([k, label]) => (
          <button key={k} role="tab" type="button" aria-selected={tab === k} className={`tvw-tab${tab === k ? " on" : ""}`} onClick={() => (setTab(k), setRotate(false))}>{label}</button>
        ))}
        <label className="tvw-switch">
          <input type="checkbox" checked={rotate} onChange={(e) => setRotate(e.target.checked)} />
          <span aria-hidden="true" />
          Rotate
        </label>
      </nav>
      {!data ? <div className="muted">Loading…</div> : (
        <>
          {tab === "overview" ? (
            <>
              {data.days?.length ? (
                <section className="tvw-days" aria-label="Incidents per day, last 7 days">
                  {data.days.map((d, i) => <DayCard key={d.date} d={d} today={i === data.days!.length - 1} />)}
                </section>
              ) : null}
              {data.hours ? (
                <section className="tvw-panel" aria-label="Last 24 hours">
                  <div className="row" style={{ justifyContent: "space-between" }}>
                    <strong style={{ fontSize: 22 }}>Last 24 hours</strong>
                    <span className="small muted">Curve: incidents open · shaded: maintenance · drops: acknowledged within 15 min</span>
                  </div>
                  <HourlyChart points={data.hours.points} maintenance={data.hours.maintenance} from={data.hours.from} to={data.hours.to} />
                </section>
              ) : null}
              {data.tiles?.length ? (
                <section className="tvw-tiles" aria-label="Status tiles">
                  {data.tiles.map((t) => <Tile key={t.key} tile={t} open={openTile === t.key} onToggle={() => setOpenTile(openTile === t.key ? "" : t.key)} />)}
                </section>
              ) : null}
            </>
          ) : null}
          {tab === "burning" ? <Burning list={burning} now={now} /> : null}
          {tab === "clock" ? (
            <section className="tvw-panel" style={{ display: "grid", gridTemplateColumns: "minmax(320px, 1fr) minmax(320px, 1fr)", gap: 28 }}>
              <IncidentClock clock={data.clock} open={c!.open} critical={c!.critical} />
              {data.daily?.length ? (
                <div className="cell-stack" style={{ gap: 8 }} aria-label="Incidents per day, last 14 days">
                  <span className="tv-label">Incidents per day · 14 days</span>
                  <DailyBars daily={data.daily} height={220} />
                </div>
              ) : null}
            </section>
          ) : null}
          {tab === "hosts" ? (
            <section className="tvw-tiles">
              {data.health.map((g) => (
                <div key={g.group} className="tvw-tile" style={{ cursor: "default" }}>
                  <span className="tvw-tile-title">{g.group}</span>
                  <span className="tvw-value">{g.up}<span className="tvw-unit">of {g.total} up</span></span>
                  <span className="tvw-word" style={{ color: g.up < g.total ? "var(--crit)" : "var(--ok)" }}>{g.up < g.total ? `${g.total - g.up} down` : "All up"}</span>
                </div>
              ))}
            </section>
          ) : null}
        </>
      )}
    </main>
  );
}
