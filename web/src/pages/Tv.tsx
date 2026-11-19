import { useEffect, useState } from "react";
import { DailyBars } from "../components/DailyBars";
import { IncidentClock } from "../components/IncidentClock";
import { getJson, type NocIncident, type TvData } from "../lib/api";
import { shortNumber, tierLabel } from "../lib/format";

const REFRESH_MS = 30_000;
const STALE_MS = 90_000;
const SEV_COLOR: Record<string, string> = { CRITICAL: "var(--crit)", HIGH: "var(--high)", WARNING: "var(--warn)" };

function age(iso: string | null, now: number) {
  if (!iso) return "";
  const minutes = Math.max(0, Math.round((now - new Date(iso).getTime()) / 60_000));
  return minutes < 60 ? `${minutes} min` : `${Math.floor(minutes / 60)} h ${minutes % 60} min`;
}

/**
 * Wall screen at /app/tv: big numbers, the 12-hour incident clock, what is burning now and who is on
 * call. No menu, refresh every 30 s. A Viewer account is enough. When updates stop, a red banner
 * says so, so an old picture never passes for a calm night.
 */
export function TvPage() {
  const [data, setData] = useState<TvData | null>(null);
  const [lastOk, setLastOk] = useState<number>(0);
  const [now, setNow] = useState(() => Date.now());
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
  const stale = lastOk > 0 && now - lastOk > STALE_MS;
  const c = data?.counts;
  const burning: NocIncident[] = data ? [...data.incidents.active, ...data.incidents.acknowledged].sort((a, b) => (a.severity === b.severity ? 0 : a.severity === "CRITICAL" ? -1 : b.severity === "CRITICAL" ? 1 : 0)).slice(0, 8) : [];
  return (
    <main className="tv">
      <header className="row" style={{ justifyContent: "space-between" }}>
        <div className="row" style={{ gap: 14 }}>
          <strong style={{ fontSize: 26 }}>ForgeSRE</strong>
          <span className="tv-label">NOC · wall screen</span>
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
      {!data ? <div className="muted">Loading…</div> : (
        <div className="tv-grid">
          <section className="cell-stack" style={{ gap: 16 }} aria-label="Key numbers">
            <div className="tv-kpi"><span className="tv-label">Open incidents</span><span className="value">{c!.open}</span><span className="muted">{c!.critical} critical · {c!.high} high · {c!.warning} warning</span></div>
            <div className="tv-kpi"><span className="tv-label">Not acknowledged</span><span className="value" style={{ color: c!.not_acked ? "var(--crit)" : "var(--text)" }}>{c!.not_acked}</span></div>
            <div className="tv-kpi"><span className="tv-label">Hosts down</span><span className="value" style={{ color: c!.hosts_down ? "var(--crit)" : "var(--ok)" }}>{c!.hosts_down}</span><span className="muted">of {c!.hosts_total}{c!.hosts_down_names.length ? ` · ${c!.hosts_down_names.join(", ")}` : ""}</span></div>
            <div className="tv-kpi"><span className="tv-label">MTTA today</span><span className="value">{c!.mtta_today_min ?? "–"}{c!.mtta_today_min !== null ? <span style={{ fontSize: 24 }}> min</span> : null}</span>{c!.mtta_today_min === null ? <span className="muted">no acknowledged incident today</span> : null}</div>
          </section>
          <section aria-label="Incidents in the last 12 hours">
            <IncidentClock clock={data.clock} open={c!.open} critical={c!.critical} />
            {data.daily?.length ? (
              <div className="cell-stack" style={{ gap: 8, marginTop: 24 }} aria-label="Incidents per day, last 14 days">
                <span className="tv-label">Incidents per day · 14 days</span>
                <DailyBars daily={data.daily} height={120} />
              </div>
            ) : null}
          </section>
          <section className="cell-stack" style={{ gap: 16 }}>
            <div className="tv-kpi">
              <span className="tv-label">On call now</span>
              <span style={{ fontSize: 28, fontWeight: 700 }}>{data.oncall?.name ?? "Nobody on the roster"}</span>
              {data.oncall?.phone ? <span className="mono" style={{ fontSize: 22 }}>{data.oncall.phone}</span> : null}
            </div>
            <div className="tv-kpi" style={{ gap: 12 }}>
              <span className="tv-label">Burning now</span>
              {burning.length === 0 ? <span className="muted" style={{ fontSize: 22 }}>Nothing open. Quiet.</span> : burning.map((i) => (
                <div key={i.number} className="cell-stack" style={{ gap: 2, borderTop: "1px solid var(--border)", paddingTop: 10 }}>
                  <span className="row" style={{ gap: 10 }}>
                    <span className="mono" style={{ color: SEV_COLOR[i.severity] ?? "var(--muted)", fontWeight: 700 }}>{i.severity}</span>
                    <span className="mono faint">{age(i.started_at, now)}</span>
                    {i.ack_by ? <span className="faint">· {i.ack_by}</span> : <span style={{ color: "var(--crit)" }}>· not acked</span>}
                  </span>
                  <span style={{ fontWeight: 600 }}>{i.title}</span>
                  <span className="muted small">{[i.asset?.hostname, shortNumber(i.number), tierLabel(i.level)].filter(Boolean).join(" · ")}</span>
                </div>
              ))}
            </div>
          </section>
        </div>
      )}
    </main>
  );
}
