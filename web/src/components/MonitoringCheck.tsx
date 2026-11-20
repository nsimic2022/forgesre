import { useEffect, useRef, useState } from "react";
import { postJson } from "../lib/api";
import { waitForJob } from "../lib/jobs";
import { Chip } from "./ui";

export type Step = { key: string; title: string; state: "ok" | "fail" | "wait" | "skip"; detail: string; fix: string };
export type Checklist = { asset_id: string; overall: string; reason: string; hint: string; class: string; steps: Step[] };

const MARK: Record<Step["state"], { text: string; color: string }> = {
  ok: { text: "✓", color: "var(--ok)" }, fail: { text: "✗", color: "var(--crit)" },
  wait: { text: "…", color: "var(--warn)" }, skip: { text: "–", color: "var(--faint)" },
};

/** Live check that ForgeSRE really watches an asset: address → exporter or SNMP → Prometheus →
 *  samples → Alertmanager → ForgeSRE. Runs as a background job; "Check again" repeats it. */
export function MonitoringCheck({ assetId, auto = false }: { assetId: string; auto?: boolean }) {
  const [list, setList] = useState<Checklist | null>(null);
  const [busy, setBusy] = useState(false);
  const [note, setNote] = useState("");
  const [error, setError] = useState("");
  const alive = useRef(true);
  useEffect(() => () => void (alive.current = false), []);
  const run = async () => {
    setBusy(true);
    setError("");
    try {
      const started = await postJson<{ job: number; snmp_apply: boolean }>(`/api/v1/assets/${encodeURIComponent(assetId)}/verify-job`, {});
      setNote(started.snmp_apply ? "Writing its SNMP credentials into snmp_exporter first." : "");
      const job = await waitForJob<Checklist>(started.job);
      if (!alive.current) return;
      if (job.status === "error") setError(job.error || "The check failed.");
      else setList(job.result);
    } catch (e) {
      if (alive.current) setError((e as Error).message);
    } finally {
      if (alive.current) setBusy(false);
    }
  };
  useEffect(() => {
    if (auto) void run();
  }, [assetId]);
  const tone = list?.overall === "PASS" ? "ok" : list?.overall === "FAIL" ? "crit" : "warn";
  return (
    <div className="cell-stack" style={{ gap: 8 }} aria-label="Monitoring check">
      <div className="row" style={{ justifyContent: "space-between" }}>
        <strong>Is it really monitored?</strong>
        <div className="row" style={{ gap: 6 }}>
          {list ? <Chip tone={tone}>{list.overall === "PASS" ? "Monitored" : list.overall === "FAIL" ? "Not working" : "Not complete yet"}</Chip> : null}
          <button className="btn" type="button" onClick={run} disabled={busy}>{busy ? "Checking…" : list ? "Check again" : "Check monitoring"}</button>
        </div>
      </div>
      {note && busy ? <span className="small faint">{note}</span> : null}
      {error ? <span className="small" role="alert" style={{ color: "var(--crit)" }}>{error}</span> : null}
      {list ? (
        <ol style={{ listStyle: "none", margin: 0, padding: 0 }}>
          {list.steps.map((s) => (
            <li key={s.key} style={{ display: "grid", gridTemplateColumns: "22px minmax(0, 1fr)", gap: 8, padding: "6px 0", borderTop: "1px solid var(--border)" }}>
              <span aria-label={s.state} style={{ color: MARK[s.state].color, fontWeight: 700 }}>{MARK[s.state].text}</span>
              <div className="cell-stack" style={{ gap: 2 }}>
                <span>{s.title}</span>
                {s.detail ? <span className="small muted">{s.detail}</span> : null}
                {s.fix ? <span className="small" style={{ color: s.state === "fail" ? "var(--crit)" : "var(--warn)" }}>{s.fix}</span> : null}
              </div>
            </li>
          ))}
        </ol>
      ) : null}
      {list?.hint ? <span className="small faint">{list.hint}</span> : null}
    </div>
  );
}
