import { useCallback, useEffect, useState } from "react";
import { Link } from "react-router-dom";
import { Card, Chip } from "../components/ui";
import { getJson, postJson } from "../lib/api";
import { clock } from "../lib/format";
import { COLOR, TONE, WORD, type FlowStep, type Lane } from "./Flow";

type FlowSummary = { name: string; label: string; about: string; configured: boolean; status: string;
  result: { checked_at: string; lanes: Lane[] } | null };

const RANK: Record<string, number> = { bad: 0, warn: 1, ok: 2, unknown: 3, off: 4 };

/** Worst block of a flow: bad, warn, ok, or unknown when it was never checked. */
export function worst(flow: FlowSummary): "bad" | "warn" | "ok" | "unknown" | "off" {
  if (!flow.configured) return "off";
  const steps = (flow.result?.lanes ?? []).flatMap((l) => l.steps);
  if (!steps.length) return "unknown";
  if (steps.some((s) => s.status === "bad")) return "bad";
  if (steps.some((s) => s.status === "warn")) return "warn";
  return "ok";
}

function MiniLane({ lane }: { lane: Lane }) {
  return (
    <div className="row" style={{ gap: 0, flexWrap: "wrap", rowGap: 4 }} role="list" aria-label={lane.title}>
      {lane.steps.map((s: FlowStep, i) => (
        <span key={s.id} className="row" style={{ gap: 0 }} role="listitem" aria-label={`${s.label}: ${WORD[s.status]}`} title={`${s.label}: ${WORD[s.status]} — ${s.detail}`}>
          {i ? <span style={{ width: 14, borderTop: `2px ${s.status === "unknown" || s.status === "off" ? "dashed" : "solid"} ${COLOR[s.status]}` }} /> : null}
          <i style={{ width: 11, height: 11, borderRadius: "50%", background: COLOR[s.status], display: "inline-block" }} />
        </span>
      ))}
    </div>
  );
}

/**
 * Integration map: every flow as a small diagram, broken ones first. A click opens the flow's own
 * page; "Check all" queues a background check of every set-up flow (read-only).
 */
export function IntegrationMapPage() {
  const [flows, setFlows] = useState<FlowSummary[] | null>(null);
  const [error, setError] = useState("");
  const [checking, setChecking] = useState(false);
  const load = useCallback(() => getJson<FlowSummary[]>("/api/v1/flows").then(setFlows).catch((e: Error) => setError(e.message)), []);
  useEffect(() => void load(), [load]);
  const busy = (flows ?? []).some((f) => f.status === "pending" || f.status === "running");
  useEffect(() => {
    if (!busy) return;
    const timer = window.setTimeout(() => void load(), 2000);
    return () => window.clearTimeout(timer);
  });
  const checkAll = async () => {
    setChecking(true);
    try {
      for (const f of (flows ?? []).filter((x) => x.configured)) await postJson(`/api/v1/integrations/${f.name}/flow`, {});
      await load();
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setChecking(false);
    }
  };
  const sorted = [...(flows ?? [])].sort((a, b) => RANK[worst(a)] - RANK[worst(b)]);
  const broken = sorted.filter((f) => worst(f) === "bad").length;
  return (
    <>
      <header className="page-head">
        <div className="cell-stack" style={{ gap: 6 }}>
          <span className="small faint"><Link to="/settings?group=integrations">Settings › Integrations</Link> / Map</span>
          <h1>Integration map</h1>
          <span className="muted">
            Every path between ForgeSRE and the systems it talks to, broken ones first. Each dot is a block of that flow; open one for its
            blocks, logs and ForgeAI. {flows ? (broken ? `${broken} flow(s) broken.` : "Nothing is broken.") : ""}
          </span>
        </div>
        <button className="btn primary" type="button" onClick={() => void checkAll()} disabled={checking || busy}>
          {checking || busy ? "Checking…" : "Check all set-up flows"}
        </button>
      </header>
      {error ? <div className="banner error" role="alert">{error}</div> : null}
      {!flows && !error ? <Card><span className="muted">Loading…</span></Card> : null}
      <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fill, minmax(300px, 1fr))", gap: 16 }}>
        {sorted.map((f) => {
          const w = worst(f);
          const first = (f.result?.lanes ?? []).flatMap((l) => l.steps).find((s) => s.status === "bad") ??
            (f.result?.lanes ?? []).flatMap((l) => l.steps).find((s) => s.status === "warn");
          return (
            <Link key={f.name} to={`/settings/integrations/${f.name}`} className="card" style={{ gap: 8, color: "inherit", textDecoration: "none", opacity: f.configured ? 1 : 0.6 }}
              aria-label={`${f.label}: ${f.configured ? WORD[w] : "Not set up"}`}>
              <div className="row" style={{ justifyContent: "space-between" }}>
                <h3 style={{ margin: 0, fontSize: 15 }}>{f.label}</h3>
                <Chip tone={TONE[w]}>{f.configured ? WORD[w] : "Not set up"}</Chip>
              </div>
              {(f.result?.lanes ?? []).map((l) => <MiniLane key={l.id} lane={l} />)}
              {first ? <span className="small" style={{ overflowWrap: "anywhere" }}><b>{first.label}:</b> {first.detail}</span> : null}
              <span className="small faint">
                {f.status === "pending" || f.status === "running" ? "Checking…" : f.result ? `Checked ${clock(f.result.checked_at)}` : f.configured ? "Not checked yet" : f.about}
              </span>
            </Link>
          );
        })}
      </div>
    </>
  );
}
