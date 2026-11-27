import { useEffect, useState } from "react";
import { getJson } from "../lib/api";
import { clock } from "../lib/format";
import { Card, Chip } from "./ui";

type Check = { key: string; label: string; status: "ok" | "warn" | "bad" | "info" | "unknown"; detail: string; fix: string };
type Overview = {
  platform: Check[];
  host: { checked_at: string; as_root: boolean; stale: boolean; checks: Check[] } | null;
  summary: Record<string, number>;
};

const TONE: Record<string, "ok" | "warn" | "crit" | "off"> = { ok: "ok", warn: "warn", bad: "crit", info: "off", unknown: "off" };
const WORD: Record<string, string> = { ok: "OK", warn: "Warning", bad: "Problem", info: "Note", unknown: "Not checked" };

/** One list of findings: status, what was found, and the fix to do by hand. */
function Checks({ rows }: { rows: Check[] }) {
  return (
    <table className="data">
      <thead><tr><th style={{ width: 120 }}>Status</th><th style={{ width: 210 }}>Check</th><th>Found</th></tr></thead>
      <tbody>
        {rows.map((c) => (
          <tr key={c.key}>
            <td><Chip tone={TONE[c.status] ?? "off"}>{WORD[c.status] ?? c.status}</Chip></td>
            <td>{c.label}</td>
            <td className="cell-stack" style={{ gap: 4 }}>
              <span>{c.detail}</span>
              {c.fix && c.status !== "ok" ? <span className="small muted">Fix: <span className="mono">{c.fix}</span></span> : null}
            </td>
          </tr>
        ))}
      </tbody>
    </table>
  );
}

/**
 * Settings › Security (Admin): read-only. Platform checks come from Core now; host checks are the
 * last report of ./forgesre security (or doctor / update) on the server. Nothing here changes anything.
 */
export function SecurityPanel() {
  const [data, setData] = useState<Overview | null>(null);
  const [error, setError] = useState("");
  useEffect(() => {
    getJson<Overview>("/api/v1/system/security").then(setData).catch((e: Error) => setError(e.message));
  }, []);
  if (error) return <Card title="Security"><div className="banner error" role="alert">{error}</div></Card>;
  if (!data) return <Card title="Security"><span className="muted">Loading…</span></Card>;
  const s = data.summary;
  return (
    <>
      <Card title="Security" actions={
        <span className="row" style={{ gap: 6 }}>
          {s.bad ? <Chip tone="crit">{s.bad} problem(s)</Chip> : null}
          {s.warn ? <Chip tone="warn">{s.warn} warning(s)</Chip> : null}
          {!s.bad && !s.warn ? <Chip tone="ok">Nothing to do</Chip> : null}
        </span>
      }>
        <span className="small muted">
          A read-only check: nothing is changed here or on the server. Each finding says what to do by hand.
          Guide: docs/install-config.md › Hardening the server.
        </span>
        <h3 className="small faint" style={{ margin: "8px 0 0", textTransform: "uppercase", letterSpacing: ".05em" }}>Platform</h3>
        <Checks rows={data.platform} />
      </Card>
      <Card title="Server" actions={data.host ? <span className="small faint">checked {clock(data.host.checked_at)}{data.host.as_root ? "" : " · without root"}</span> : null}>
        {data.host ? (
          <>
            {data.host.stale ? <div className="banner" role="status">This report is more than a week old. On the server: <span className="mono">sudo ./forgesre security</span></div> : null}
            {!data.host.as_root ? <span className="small muted">Checked without root, so SSH and firewall may show "Not checked". On the server: <span className="mono">sudo ./forgesre security</span></span> : null}
            <Checks rows={data.host.checks} />
          </>
        ) : (
          <span className="muted">No server report yet. On the server run <span className="mono">sudo ./forgesre security</span> (doctor and update also refresh it).</span>
        )}
      </Card>
    </>
  );
}
