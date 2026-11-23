import { useEffect, useState } from "react";
import { getJson } from "../lib/api";
import { Card, Chip } from "./ui";

type UpsRow = {
  asset: string; state: string; source: string; battery: string; charge: number | null; minutes_left: number | null;
  load: number | null; input_v: number | null; battery_temp: number | null; alarms: number;
};
type Sensor = { asset: string; name: string; kind: string; unit: string; value: number; state: string };
export type PowerData = { ups: UpsRow[]; sensors: Sensor[]; silent: string[]; error: string };

const tone = (s: string) => (s === "critical" ? "crit" : s === "warning" ? "warn" : "ok");
const num = (v: number | null | undefined, suffix = "") => (v === null || v === undefined ? "–" : `${Math.round(v * 10) / 10}${suffix}`);

/** NOC "Power and environment": UPS state and runtime, then sensor readings. Hidden when there are no power assets. */
export function PowerPanel() {
  const [data, setData] = useState<PowerData | null>(null);
  useEffect(() => {
    const load = () => getJson<PowerData>("/api/v1/power").then(setData).catch(() => setData(null));
    void load();
    const t = window.setInterval(load, 60_000);
    return () => window.clearInterval(t);
  }, []);
  const ups = Array.isArray(data?.ups) ? data!.ups : [];
  const sensors = Array.isArray(data?.sensors) ? data!.sensors : [];
  const silent = Array.isArray(data?.silent) ? data!.silent : [];
  if (!data || (!ups.length && !sensors.length && !silent.length)) return null;
  return (
    <Card title="Power and environment">
      {data.error ? <div className="banner error" role="alert">Prometheus: {data.error}</div> : null}
      {ups.length ? (
        <table className="table" aria-label="UPS">
          <thead><tr><th>UPS</th><th>State</th><th>Source</th><th>Battery</th><th>Charge</th><th>Runtime</th><th>Load</th><th>Input</th></tr></thead>
          <tbody>
            {ups.map((r) => (
              <tr key={r.asset}>
                <td className="mono">{r.asset}</td>
                <td><Chip tone={tone(r.state)}>{r.source === "battery" ? "On battery" : r.state}</Chip>{r.alarms ? <span className="small muted"> {r.alarms} alarm(s)</span> : null}</td>
                <td>{r.source}</td>
                <td>{r.battery}{r.battery_temp !== null ? <span className="small muted"> · {num(r.battery_temp, " °C")}</span> : null}</td>
                <td>{num(r.charge, "%")}</td>
                <td>{num(r.minutes_left, " min")}</td>
                <td>{num(r.load, "%")}</td>
                <td>{num(r.input_v, " V")}</td>
              </tr>
            ))}
          </tbody>
        </table>
      ) : null}
      {sensors.length ? (
        <div className="row" style={{ gap: 8, flexWrap: "wrap", marginTop: 8 }} aria-label="Sensors">
          {sensors.map((s) => (
            <span key={`${s.asset}-${s.name}`} className="cell-stack" style={{ minWidth: 140 }}>
              <span className="small muted">{s.asset} · {s.name}</span>
              <Chip tone={tone(s.state)}>{num(s.value)} {s.unit}</Chip>
            </span>
          ))}
        </div>
      ) : null}
      {silent.length ? <div className="small muted" style={{ marginTop: 8 }}>No SNMP data yet from: {silent.join(", ")}</div> : null}
    </Card>
  );
}
