import { useEffect, useMemo, useState } from "react";
import { getJson } from "../lib/api";
import { Card, Chip } from "./ui";

type Container = { id: string; name: string; service: string; image: string; state: string; status: string; health: string; done?: boolean };
type Logs = { service: string; name: string; state: string; status: string; tail: number; lines: string[] };

const ERR = /\b(error|err|fail(ed|ure)?|fatal|panic|exception|traceback|refused|timeout|denied)\b/i;
// a one-shot job that finished (netbox-db-init, exit code 0) is fine, not "down"
const tone = (c: Container) => (c.done ? "off" : c.state !== "running" || c.health === "unhealthy" ? "crit" : c.health === "starting" ? "warn" : "ok");

/**
 * Settings › Containers (Admin): every container of the appliance with its state and health, and the
 * last lines of one container's log (passwords and tokens masked by the server). Read only, through
 * the docker-ro helper; the same as ./forgesre status and ./forgesre logs SERVICE.
 */
export function ContainersPanel() {
  const [rows, setRows] = useState<Container[] | null>(null);
  const [error, setError] = useState("");
  const [picked, setPicked] = useState("");
  const [tail, setTail] = useState(200);
  const [logs, setLogs] = useState<Logs | null>(null);
  const [logError, setLogError] = useState("");
  const [filter, setFilter] = useState("");
  const [errorsOnly, setErrorsOnly] = useState(false);
  const load = () => getJson<{ containers: Container[]; error: string }>("/api/v1/system/containers")
    .then((d) => (setRows(d.containers), setError(d.error))).catch((e: Error) => setError(e.message));
  const readLogs = (service: string, n = tail) => {
    setPicked(service);
    setLogError("");
    getJson<Logs>(`/api/v1/system/containers/${encodeURIComponent(service)}/logs?tail=${n}`).then(setLogs).catch((e: Error) => (setLogs(null), setLogError(e.message)));
  };
  useEffect(() => void load(), []);
  const lines = useMemo(() => (logs?.lines ?? []).filter((l) => (!errorsOnly || ERR.test(l)) && (!filter || l.toLowerCase().includes(filter.toLowerCase()))), [logs, filter, errorsOnly]);
  const down = (rows ?? []).filter((c) => tone(c) === "crit").length;
  return (
    <>
      <Card title="Containers" actions={<>{rows ? <Chip tone={down ? "crit" : "ok"}>{down ? `${down} not running` : `${rows.filter((c) => !c.done).length} running`}</Chip> : null}<button className="btn" type="button" onClick={() => void load()}>Refresh</button></>}>
        <span className="small muted">Every program of the appliance runs in its own container. Read only: nothing can be started or stopped from here
          (<code className="mono">./forgesre restart</code> on the server does that). Same as <code className="mono">./forgesre status</code>.</span>
        {error ? <div className="banner error" role="alert">{error}</div> : null}
        {rows && rows.length ? (
          <div className="table-wrap"><table className="data" style={{ minWidth: 720 }}>
            <thead><tr><th style={{ width: 190 }}>Service</th><th style={{ width: 110 }}>State</th><th>Status</th><th>Image</th><th style={{ width: 110 }}></th></tr></thead>
            <tbody>
              {rows.map((c) => (
                <tr key={c.id} aria-selected={picked === c.service}>
                  <td style={{ fontWeight: 600 }}>{c.service}</td>
                  <td><Chip tone={tone(c)}>{c.done ? "finished" : c.health && c.state === "running" ? c.health : c.state}</Chip></td>
                  <td className="small muted">{c.status}</td>
                  <td className="small mono faint">{c.image}</td>
                  <td><button className="btn" type="button" onClick={() => readLogs(c.service)}>Logs</button></td>
                </tr>
              ))}
            </tbody>
          </table></div>
        ) : rows ? <span className="muted">No containers found.</span> : <span className="muted">Loading…</span>}
      </Card>
      {picked ? (
        <Card title={`Logs · ${picked}`} actions={
          <div className="row" style={{ gap: 6 }}>
            <select aria-label="Lines" value={tail} onChange={(e) => (setTail(Number(e.target.value)), readLogs(picked, Number(e.target.value)))}>
              {[100, 200, 500, 1000].map((n) => <option key={n} value={n}>last {n} lines</option>)}
            </select>
            <button className="btn" type="button" onClick={() => readLogs(picked)}>Refresh</button>
            <button className="btn" type="button" onClick={() => (setPicked(""), setLogs(null))}>Close</button>
          </div>
        }>
          <div className="row" style={{ gap: 8 }}>
            <input className="search" type="search" aria-label="Filter log lines" placeholder="Filter lines" value={filter} onChange={(e) => setFilter(e.target.value)} style={{ maxWidth: 320 }} />
            <label className="row small" style={{ gap: 6 }}><input type="checkbox" checked={errorsOnly} onChange={(e) => setErrorsOnly(e.target.checked)} /> Errors only</label>
            {logs ? <span className="small faint">{lines.length} of {logs.lines.length} lines · passwords and tokens are masked</span> : null}
          </div>
          {logError ? <div className="banner error" role="alert">{logError}</div> : null}
          {logs ? (
            <pre className="log-view" aria-label={`Log of ${picked}`}>
              {lines.length ? lines.map((l, i) => <span key={i} className={ERR.test(l) ? "log-err" : undefined}>{l}{"\n"}</span>) : "Nothing to show."}
            </pre>
          ) : !logError ? <span className="muted">Loading…</span> : null}
          <span className="small faint">On the server: <code className="mono">./forgesre logs {picked}</code> · live: <code className="mono">docker compose logs -f --tail 200 {picked}</code></span>
        </Card>
      ) : null}
    </>
  );
}
