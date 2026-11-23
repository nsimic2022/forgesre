import { useEffect, useState } from "react";
import { getJson } from "../lib/api";
import { usePaged } from "./Pager";
import { Card } from "./ui";

type Row = { alertname: string; incidents: number; acked_pct: number; self_cleared_pct: number; flapping: number; linked: number; median_minutes: number | null; assets: number; advice: string };
type Data = { days: number; incidents: number; actionable_pct: number | null; rows: Row[] };

/** Which alerts page people for nothing: noisiest first, with one plain suggestion each. */
export function AlertQuality({ days }: { days: number }) {
  const [data, setData] = useState<Data | null>(null);
  const [error, setError] = useState("");
  useEffect(() => void getJson<Data>(`/api/v1/reports/alert-quality?days=${days}`).then((d) => setData({ ...d, rows: Array.isArray(d?.rows) ? d.rows : [] })).catch((e: Error) => setError(e.message)), [days]);
  const pg = usePaged(data?.rows ?? [], "alert-quality", "alerts");
  return (
    <Card title="Alert quality"
      actions={data?.actionable_pct != null ? <span className="small faint">{data.actionable_pct}% of {data.incidents} incidents acknowledged by a person</span> : null}>
      {error ? <span className="small" role="alert">{error}</span> : !data ? <span className="muted">Loading…</span> : data.rows.length === 0 ? (
        <span className="muted">No incidents in this period.</span>
      ) : (
        <>
          <div className="table-wrap">
            <table className="data" style={{ minWidth: 640 }}>
              <thead><tr><th>Alert</th><th>Incidents</th><th>Acknowledged</th><th>Cleared alone</th><th>Flapping</th><th>Suggestion</th></tr></thead>
              <tbody>
                {pg.page.map((r) => (
                  <tr key={r.alertname}>
                    <td className="mono">{r.alertname}</td>
                    <td>{r.incidents}</td>
                    <td>{r.acked_pct}%</td>
                    <td style={{ color: r.self_cleared_pct >= 70 ? "var(--warn)" : undefined }}>{r.self_cleared_pct}%</td>
                    <td>{r.flapping || "—"}</td>
                    <td className="small">{r.advice}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
          {pg.pager}
        </>
      )}
    </Card>
  );
}
