import { useEffect, useRef, useState } from "react";
import { Link } from "react-router-dom";
import { getJson, postJson } from "../lib/api";
import { Card } from "./ui";

type Scenario = { key: string; label: string; detail: string };
type DemoJob = { status: string; error: string; result: { incident: string | null } | null };

/** Settings › Demo (Admin): open a lab incident on a forge-demo-* asset to show the whole path,
 *  alarm → ForgeRCA → escalation mail. Runs as a background job; nothing touches a real host. */
export function DemoPanel() {
  const [scenarios, setScenarios] = useState<Scenario[]>([]);
  const [busy, setBusy] = useState("");
  const [opened, setOpened] = useState<{ label: string; incident: string } | null>(null);
  const [note, setNote] = useState("");
  const [error, setError] = useState("");
  const alive = useRef(true);
  useEffect(() => () => void (alive.current = false), []);
  useEffect(() => {
    getJson<{ scenarios: Scenario[] }>("/api/v1/demo/scenarios").then((d) => setScenarios(d.scenarios)).catch((e: Error) => setError(e.message));
  }, []);

  const run = async (s: Scenario) => {
    setBusy(s.key);
    setError("");
    setNote("");
    setOpened(null);
    try {
      const { job } = await postJson<{ job: number }>("/api/v1/demo/run", { scenario: s.key });
      for (let i = 0; i < 60 && alive.current; i++) {
        const state = await getJson<DemoJob>(`/api/v1/demo/jobs/${job}`);
        if (state.status === "done") return void (state.result?.incident ? setOpened({ label: s.label, incident: state.result.incident }) : setNote("The scenario ran but opened no incident (a noise filter or maintenance window may hold it)."));
        if (state.status === "error") return void setError(state.error || "The scenario failed.");
        await new Promise((r) => setTimeout(r, 1000));
      }
    } catch (e) {
      if (alive.current) setError((e as Error).message);
    } finally {
      if (alive.current) setBusy("");
    }
  };
  const reset = async () => {
    setBusy("reset");
    setError("");
    try {
      await postJson("/api/v1/demo-reset", {});
      setNote("Demo CPU and disk gauges lowered; the demo alarms resolve on their next evaluation.");
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setBusy("");
    }
  };

  return (
    <Card title="Demo scenarios">
      <span className="small muted">
        Each button opens a DEMO-tagged incident on a seeded lab asset (forge-demo-*), runs ForgeRCA and the first escalation step, as a new alarm would.
        Mail subjects carry DEMO. Real hosts are never touched. Same as <code className="mono">./forgesre demo</code>.
      </span>
      {scenarios.map((s) => (
        <div key={s.key} className="row" style={{ justifyContent: "space-between", borderTop: "1px solid var(--border)", paddingTop: 8 }}>
          <div className="cell-stack">
            <span style={{ fontWeight: 600 }}>{s.label}</span>
            <span className="small muted">{s.detail}</span>
          </div>
          <button className="btn" type="button" disabled={!!busy} onClick={() => void run(s)}>{busy === s.key ? "Opening…" : "Run"}</button>
        </div>
      ))}
      <div className="row" style={{ borderTop: "1px solid var(--border)", paddingTop: 8 }}>
        <button className="btn" type="button" disabled={!!busy} onClick={() => void reset()}>Lower demo gauges</button>
      </div>
      {opened ? (
        <div className="banner" role="status" style={{ background: "var(--ok-bg)" }}>
          {opened.label}: opened <Link to={`/incidents/${encodeURIComponent(opened.incident)}`}>{opened.incident}</Link>.
        </div>
      ) : null}
      {note ? <div className="banner" role="status">{note}</div> : null}
      {error ? <div className="banner error" role="alert">{error}</div> : null}
    </Card>
  );
}
