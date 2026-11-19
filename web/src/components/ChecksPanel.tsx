import { useEffect, useState } from "react";
import { getJson, postJson } from "../lib/api";
import { clock } from "../lib/format";
import { Card, Chip } from "./ui";

type Result = { kind: string; target: string; status: string; value: number | null; detail: string; checked_at: string | null };
type Checks = { results: Result[]; heartbeats: { name: string; every_hours: number; asset: string; note: string; last_at: string | null }[]; tls_targets: string[]; can_edit: boolean };
const TONE: Record<string, "ok" | "warn" | "crit" | "high"> = { ok: "ok", warning: "warn", critical: "crit", error: "high" };

/** Settings › Checks: latest certificate, backup and heartbeat results (refreshed every 15 min). */
export function ChecksPanel() {
  const [data, setData] = useState<Checks | null>(null);
  const [notice, setNotice] = useState("");
  const [error, setError] = useState("");
  const load = () => getJson<Checks>("/api/v1/checks").then(setData).catch((e: Error) => setError(e.message));
  useEffect(() => {
    load();
  }, []);
  const rows = [...(data?.results ?? [])].sort((a, b) => Number(a.status === "ok") - Number(b.status === "ok"));
  return (
    <Card title="Latest results" actions={data?.can_edit ? <button className="btn" type="button" onClick={() => postJson("/api/v1/checks/run", {}).then(() => setNotice("Running now; reload in a few seconds."))}>Run now</button> : null}>
      {notice ? <div className="banner" role="status" style={{ background: "var(--ok-bg)" }}>{notice}</div> : null}
      {error ? <div className="banner error" role="alert">{error}</div> : null}
      {rows.length === 0 ? <span className="muted">No check has run yet.</span> : (
        <div className="table-wrap">
          <table className="data">
            <thead><tr><th style={{ width: 110 }}>Status</th><th style={{ width: 100 }}>Check</th><th>Target</th><th>Detail</th><th style={{ width: 120 }}>Checked</th></tr></thead>
            <tbody>
              {rows.map((r) => (
                <tr key={`${r.kind}-${r.target}`}>
                  <td><Chip tone={TONE[r.status] ?? "off"}>{r.status}</Chip></td>
                  <td className="small">{r.kind}</td>
                  <td className="mono small">{r.target}</td>
                  <td className="small muted">{r.detail}</td>
                  <td className="small faint">{clock(r.checked_at)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
      <span className="small faint">
        Heartbeat from a backup or cron job, after each success:{" "}
        <code className="mono">curl -fsS -X POST -H "Authorization: Bearer $CHANGES_WEBHOOK_TOKEN" "https://forgesre/api/v1/heartbeats/veeam-nightly?every_hours=24&amp;asset=nas-bkp-01"</code>
      </span>
    </Card>
  );
}
