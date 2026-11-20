import { useEffect, useRef, useState } from "react";
import { getJson } from "../lib/api";
import { Chip } from "./ui";

export type CapacityRow = { label: string; state: string; note: string; spark: string; forecast_days: number | null; points: number };
export type Capacity =
  | { state: "pending"; job: number | null }
  | { state: "error"; error: string }
  | { state: "ready"; rows: CapacityRow[]; measured_at: string; soonest_days: number | null; warn: boolean };

const WARN_DAYS = 30;

/** Last 30 days of disk use and interface receive for one asset, with "~N days to full".
 *  The server reads Prometheus / Zabbix in a background job; this polls until it is ready. */
export function CapacityCard({ assetId }: { assetId: string }) {
  const [data, setData] = useState<Capacity | null>(null);
  const [error, setError] = useState("");
  const alive = useRef(true);
  useEffect(() => () => void (alive.current = false), []);

  const load = async (refresh = false) => {
    setError("");
    const path = `/api/v1/assets/${encodeURIComponent(assetId)}/capacity`;
    try {
      let next = await getJson<Capacity>(refresh ? `${path}?refresh=true` : path);
      for (let i = 0; i < 40 && next?.state === "pending" && alive.current; i++) {
        setData(next);
        await new Promise((r) => setTimeout(r, 1500));
        next = await getJson<Capacity>(path);
      }
      if (alive.current) setData(next);
    } catch (e) {
      if (alive.current) setError((e as Error).message);
    }
  };
  useEffect(() => {
    setData(null);
    void load();
  }, [assetId]); // eslint-disable-line react-hooks/exhaustive-deps

  const head = (
    <h3 className="small faint" style={{ margin: 0, textTransform: "uppercase", letterSpacing: ".05em" }}>Capacity, last 30 days</h3>
  );
  if (error) return <>{head}<span className="small" style={{ color: "var(--crit)" }}>{error}</span></>;
  if (!data || data.state === "pending") return <>{head}<span className="small muted">Reading 30 days from Prometheus…</span></>;
  if (data.state === "error") {
    return (
      <>
        {head}
        <span className="small" style={{ color: "var(--crit)" }}>Could not read capacity: {data.error}</span>
        <div className="row"><button className="btn" type="button" onClick={() => void load(true)}>Read again</button></div>
      </>
    );
  }
  if (data.state !== "ready") return null;
  const rows = data.rows ?? [];
  return (
    <>
      {head}
      {rows.length === 0 ? <span className="small muted">No disk or interface series for this asset.</span> : null}
      {rows.map((r) => {
        const soon = r.forecast_days !== null && r.forecast_days <= WARN_DAYS;
        return (
          <div key={r.label} className="row" style={{ justifyContent: "space-between", gap: 12 }}>
            <span className="small" style={{ minWidth: 130 }}>{r.label}</span>
            {r.spark ? (
              <svg width="80" height="22" viewBox="0 0 80 22" role="img" aria-label={`${r.label}: ${r.note}`}>
                <polyline points={r.spark} fill="none" stroke={soon ? "var(--crit)" : "var(--accent)"} strokeWidth="1.5" />
              </svg>
            ) : <span style={{ width: 80 }} />}
            {soon ? <Chip tone="crit">{r.note}</Chip> : <span className="small muted" style={{ minWidth: 120, textAlign: "right" }}>{r.note}</span>}
          </div>
        );
      })}
      <div className="row small faint" style={{ justifyContent: "space-between" }}>
        <span>Days to full is a straight line through the last 30 days of disk use.</span>
        <button className="btn" type="button" onClick={() => void load(true)}>Read again</button>
      </div>
    </>
  );
}
