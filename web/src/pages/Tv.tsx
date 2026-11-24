import { useEffect, useMemo, useRef, useState } from "react";
import { DayClock, IncidentClock } from "../components/IncidentClock";
import { HourlyStrip, MiniTile, WeekStrip } from "../components/TvWeather";
import { getJson, type NocIncident, type TvData, type TvDayDetail } from "../lib/api";
import { shortNumber, tierLabel } from "../lib/format";
import { sevClass } from "../lib/sevcells";
import { chosenTheme, showTheme } from "../lib/theme";

const REFRESH_MS = 30_000;
const STALE_MS = 90_000;
const FRESH_MS = 60_000;
const MAX_ROWS = 8;
const PAGE_MS = 15_000; // Burning now turns to the next page by itself on the wall
const DAY_MS = 180_000; // a picked day goes back to the live clock after 3 minutes
const SEV_COLOR: Record<string, string> = { CRITICAL: "var(--crit)", HIGH: "var(--high)", WARNING: "var(--warn)" };

function clockTime(at: number, timeZone?: string): string {
  try {
    return new Date(at).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", hourCycle: "h23", timeZone });
  } catch {
    return new Date(at).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", hourCycle: "h23" });
  }
}

function age(iso: string | null, now: number) {
  if (!iso) return "";
  const minutes = Math.max(0, Math.round((now - new Date(iso).getTime()) / 60_000));
  return minutes < 60 ? `${minutes} min` : `${Math.floor(minutes / 60)} h ${minutes % 60} min`;
}

/** Oldest first within each severity, criticals on top, unacknowledged before acknowledged. */
const SEV_RANK: Record<string, number> = { CRITICAL: 0, HIGH: 1, WARNING: 2 };
function ordered(list: NocIncident[]): NocIncident[] {
  return [...list].sort((a, b) =>
    (SEV_RANK[a.severity] ?? 3) - (SEV_RANK[b.severity] ?? 3)
    || Number(Boolean(a.ack_by)) - Number(Boolean(b.ack_by))
    || (a.started_at ?? "").localeCompare(b.started_at ?? ""));
}

/**
 * Burning now: the main panel. New criticals glow for a minute; calm when nothing is open. More than
 * MAX_ROWS incidents are split into pages that turn every PAGE_MS (dots and arrows below to pick one);
 * on a small screen the list scrolls.
 */
function Burning({ list, now, fresh, lastResolved }: { list: NocIncident[]; now: number; fresh: Set<string>; lastResolved: string | null }) {
  const pages = Math.max(1, Math.ceil(list.length / MAX_ROWS));
  const [page, setPage] = useState(0);
  const [held, setHeld] = useState(0);
  useEffect(() => {
    if (pages < 2) return;
    const turn = window.setInterval(() => {
      if (Date.now() - held > PAGE_MS) setPage((p) => (p + 1) % pages);
    }, PAGE_MS);
    return () => window.clearInterval(turn);
  }, [pages, held]);
  const current = Math.min(page, pages - 1);
  if (list.length === 0) {
    return (
      <div className="tvw-quiet">
        <span className="tvw-quiet-word">All quiet</span>
        <span className="muted">Nothing open{lastResolved ? ` · last incident resolved ${age(lastResolved, now)} ago` : ""}</span>
      </div>
    );
  }
  const go = (p: number) => (setPage((p + pages) % pages), setHeld(Date.now()));
  const shown = list.slice(current * MAX_ROWS, current * MAX_ROWS + MAX_ROWS);
  return (
    <>
      <ol className="tvw-rows" aria-label={`${list.length} open incidents${pages > 1 ? `, page ${current + 1} of ${pages}` : ""}`}>
        {shown.map((i) => (
          <li key={i.number} className={`tvw-row${fresh.has(i.number) ? " fresh" : ""}`} style={{ borderLeftColor: SEV_COLOR[i.severity] ?? "var(--faint)" }}>
            <span className="tvw-row-sev" style={{ color: SEV_COLOR[i.severity] ?? "var(--muted)" }}>{i.severity}</span>
            <span className="tvw-row-main">
              <span className={`tvw-row-title ${sevClass(i.severity)}`}>{i.title}</span>
              <span className="tvw-row-meta">{[i.asset?.hostname, shortNumber(i.number), tierLabel(i.level)].filter(Boolean).join(" · ")}</span>
            </span>
            <span className="tvw-row-age">{age(i.started_at, now)}</span>
            {i.ack_by ? <span className="tvw-row-ack">{i.ack_by}</span> : <span className="tvw-row-ack not">not acked</span>}
          </li>
        ))}
      </ol>
      {pages > 1 ? (
        <nav className="tvw-pager" aria-label="Pages of open incidents">
          <button type="button" onClick={() => go(current - 1)} aria-label="Previous page">‹</button>
          {Array.from({ length: pages }, (_, n) => (
            <button type="button" key={n} className={`tvw-dot${n === current ? " on" : ""}`} onClick={() => go(n)} aria-label={`Page ${n + 1}`} aria-current={n === current ? "page" : undefined} />
          ))}
          <span className="faint">{current + 1} / {pages}</span>
          <button type="button" onClick={() => go(current + 1)} aria-label="Next page">›</button>
        </nav>
      ) : null}
    </>
  );
}

/** Bottom tiles in the order the server sends, with the shift tile moved to the far right. */
export function tileOrder<T extends { key: string }>(tiles: T[]): T[] {
  return [...tiles.filter((t) => t.key !== "shift"), ...tiles.filter((t) => t.key === "shift")];
}

/**
 * Wall screen at /app/tv: everything on one 16:9 screen, no tabs and no scrolling, sized from the
 * screen so it reads the same on a monitor and a 65" 4K TV. Burning incidents take two thirds;
 * the week (click a day for its AM / PM clock), the 12-hour clock and the host groups sit beside them; a slim 24-hour strip and a row of
 * small tiles close the bottom. Refresh every 30 s with a Viewer account; when updates stop a red
 * banner says so. Colours follow the browser's theme; ?theme=contrast picks another, and a browser
 * that never chose one gets Navy.
 */
export function TvPage() {
  const [data, setData] = useState<TvData | null>(null);
  const [lastOk, setLastOk] = useState<number>(0);
  const [now, setNow] = useState(() => Date.now());
  const seen = useRef<Map<string, number> | null>(null);
  const [picked, setPicked] = useState<string | null>(null);
  const [dayData, setDayData] = useState<TvDayDetail | null>(null);
  useEffect(() => {
    if (!picked) return setDayData(null);
    let alive = true;
    getJson<TvDayDetail>(`/api/v1/tv/day?date=${picked}`).then((d) => alive && setDayData(d)).catch(() => alive && setPicked(null));
    const back = window.setTimeout(() => setPicked(null), DAY_MS);
    return () => (alive = false, window.clearTimeout(back));
  }, [picked]);
  useEffect(() => {
    const before = document.documentElement.dataset.theme;
    showTheme(new URLSearchParams(window.location.search).get("theme") || chosenTheme() || "navy");
    return () => {
      if (before) document.documentElement.dataset.theme = before;
      else delete document.documentElement.dataset.theme;
    };
  }, []);
  useEffect(() => {
    let alive = true;
    const load = () => getJson<TvData>("/api/v1/tv").then((d) => alive && (setData(d), setLastOk(Date.now()))).catch(() => undefined);
    load();
    const poll = window.setInterval(load, REFRESH_MS);
    const tick = window.setInterval(() => setNow(Date.now()), 5_000);
    return () => {
      alive = false;
      window.clearInterval(poll);
      window.clearInterval(tick);
    };
  }, []);
  const stale = lastOk > 0 && now - lastOk > STALE_MS;
  const c = data?.counts;
  const burning = useMemo(() => (data ? ordered([...data.incidents.active, ...data.incidents.acknowledged]) : []), [data]);
  // Criticals that appeared after the page loaded glow for FRESH_MS (not the ones already open at load).
  const fresh = useMemo(() => {
    const at = Date.now();
    if (seen.current === null) {
      seen.current = new Map(burning.map((i) => [i.number, 0]));
      return new Set<string>();
    }
    for (const i of burning) if (!seen.current.has(i.number)) seen.current.set(i.number, i.severity === "CRITICAL" ? at : 0);
    return new Set([...seen.current].filter(([, t]) => t && at - t < FRESH_MS).map(([n]) => n));
  }, [burning, now]); // eslint-disable-line react-hooks/exhaustive-deps
  const lastResolved = data?.incidents.resolved_today?.map((i) => i.resolved_at).filter((v): v is string => Boolean(v)).sort().pop() ?? null;
  // the appliance's time zone, like the 12-hour clock, not the TV computer's
  const time = clockTime(now, data?.clock.timezone);
  return (
    <main className="tv tvw">
      <div className="tvw-head">
      <header className="tvw-top">
        <strong className="tvw-brand">ForgeSRE</strong>
        <span className="tvw-clock">{time}</span>
        {c ? (
          <span className="tvw-kpis">
            <span className="tvw-pill">{c.open} open</span>
            <span className={`tvw-pill${c.not_acked ? " crit" : ""}`}>{c.not_acked} not acked</span>
            <span className={`tvw-pill${c.critical ? " crit" : ""}`}>{c.critical} critical</span>
            <span className={`tvw-pill${c.hosts_down ? " crit" : " ok"}`}>{c.hosts_down} hosts down</span>
          </span>
        ) : null}
        <span className="tvw-oncall">On call: <strong>{data?.oncall?.name ?? "nobody"}</strong>{data?.oncall?.phone ? ` · ${data.oncall.phone}` : ""}</span>
        <span className="tvw-live" style={{ color: stale ? "var(--crit)" : "var(--ok)" }}>
          <i aria-hidden="true" style={{ background: stale ? "var(--crit)" : "var(--ok)" }} />{stale ? "No updates" : "Live"}
        </span>
        <a className="tvw-exit" href="/app/">Exit</a>
      </header>
      {stale ? (
        <div className="banner error tvw-stale" role="alert">
          No update from ForgeSRE since {new Date(lastOk).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", hourCycle: "h23" })}. What you see may be old.
        </div>
      ) : null}
      {data?.major?.length ? (
        <div className="major-banner tvw-major" role="alert">
          <strong>MAJOR INCIDENT</strong>
          {data.major.map((m) => <span key={m.number}>{m.number.split("_")[0]} {m.title} · commander {m.commander || "not named"}{m.overdue ? " · update overdue" : ""}</span>)}
        </div>
      ) : null}
      </div>
      {!data ? <div className="muted">Loading…</div> : (
        <>
          <section className="tvw-middle">
            <div className="tvw-card tvw-burn">
              <div className="tvw-card-head">
                <span>Burning now</span>
                <span className="faint">{burning.length ? `${burning.length} open` : ""}</span>
              </div>
              <Burning list={burning} now={now} fresh={fresh} lastResolved={lastResolved} />
            </div>
            <div className="tvw-side">
              {data.days?.length ? <WeekStrip days={data.days} picked={picked} onPick={setPicked} /> : null}
              <div className="tvw-card tvw-ring">
                {picked && dayData?.date === picked
                  ? <DayClock key={picked} day={dayData} onClose={() => setPicked(null)} />
                  : <IncidentClock clock={data.clock} open={c!.open} critical={c!.critical} />}
              </div>
              {data.health.length ? (
                <div className="tvw-hosts" aria-label="Hosts by group">
                  {data.health.map((g) => (
                    <span key={g.group} className={`tvw-host${g.up < g.total ? " down" : ""}`}>{g.group} <strong>{g.up}/{g.total}</strong></span>
                  ))}
                </div>
              ) : null}
            </div>
          </section>
          {data.hours ? <HourlyStrip points={data.hours.points} maintenance={data.hours.maintenance} from={data.hours.from} to={data.hours.to} /> : <div />}
          {data.tiles?.length ? (
            <section className="tvw-minis" style={{ gridTemplateColumns: `repeat(${data.tiles.length}, minmax(0, 1fr))` }} aria-label="Status tiles">
              {tileOrder(data.tiles).map((t) => <MiniTile key={t.key} tile={t} />)}
            </section>
          ) : <div />}
        </>
      )}
    </main>
  );
}
