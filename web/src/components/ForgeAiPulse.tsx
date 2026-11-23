import { useCallback, useEffect, useState } from "react";
import { getJson, postJson } from "../lib/api";
import { clock } from "../lib/format";

export type Pulse = {
  local: { on: boolean; model: string; reachable: boolean | null; why: string };
  test: { status: "none" | "pending" | "running" | "ok" | "error"; ms?: number; first_ms?: number; error?: string; at?: string | null };
  lane: { pending: number; running: { id: number; kind: string; seconds: number } | null };
  worker_age_s: number | null;
  timeout_s: number;
  thinking: boolean;
  cloud: { enabled: boolean; ready: boolean; provider: string; model: string };
  can_test?: boolean;
};

type Tone = "ok" | "warn" | "crit" | "off";
const secs = (ms = 0) => `${(ms / 1000).toFixed(1)} s`;

function Dot({ tone, label, title, children }: { tone: Tone; label: string; title?: string; children?: React.ReactNode }) {
  return (
    <span className={`pulse-item ${tone}`} title={title}>
      <i aria-hidden="true" />
      <span className="faint">{label}</span> {children}
    </span>
  );
}

/**
 * The small status line above a ForgeAI chat: which model, is the connection up, did the last test
 * answer (and how fast), local or cloud, and the ForgeAI queue. "Test" asks the model one tiny
 * question in the background; the line refreshes itself, faster while a test runs.
 */
export function ForgeAiPulse({ onPulse }: { onPulse?: (p: Pulse) => void }) {
  const [p, setP] = useState<Pulse | null>(null);
  const [error, setError] = useState("");
  const load = useCallback(
    () => getJson<Pulse>("/api/v1/forgeai/pulse").then((d) => (d?.local ? (setP(d), setError(""), onPulse?.(d)) : undefined)).catch((e: Error) => setError(e.message)),
    [onPulse],
  );
  const testing = p?.test.status === "pending" || p?.test.status === "running";
  useEffect(() => {
    void load();
    const t = setInterval(() => void load(), testing ? 3000 : 20000);
    return () => clearInterval(t);
  }, [load, testing]);
  const test = () => postJson("/api/v1/forgeai/ping", {}).then(() => load()).catch((e: Error) => setError(e.message));

  if (!p) return <div className="ai-pulse small faint">{error || "Checking ForgeAI…"}</div>;
  const { local, lane } = p;
  const conn: Tone = !local.on ? "off" : local.reachable ? "ok" : "crit";
  const t = p.test;
  const testTone: Tone = t.status === "ok" ? "ok" : t.status === "error" ? "crit" : testing ? "warn" : "off";
  const testText = t.status === "ok" ? `ok · ${secs(t.ms)}` : t.status === "error" ? "failed" : testing ? (t.status === "pending" ? "queued…" : "running…") : "not run";
  const workerLate = p.worker_age_s === null || (p.worker_age_s > 180 && !lane.running);
  return (
    <div className="ai-pulse small" aria-label="ForgeAI status">
      <Dot tone={conn} label="model" title={local.on ? `llama.cpp ${local.reachable ? "answers" : `does not answer: ${local.why}`}` : "ForgeAI is off (Settings › RCA / AI)"}>
        {local.model || "-"}
      </Dot>
      <Dot tone={conn} label="connection">{!local.on ? "off" : local.reachable ? "ok" : "down"}</Dot>
      <Dot tone={testTone} label="test"
        title={t.status === "ok" ? `first token after ${secs(t.first_ms)}, whole answer ${secs(t.ms)}${t.at ? ` · ${clock(t.at)}` : ""}` : t.error || "Ask the model one tiny question"}>
        {testText}
        {p.can_test && local.on && !testing ? <button type="button" className="pulse-btn" onClick={() => void test()} aria-label="Test the model now">↻</button> : null}
      </Dot>
      <Dot tone={p.cloud.enabled && p.cloud.ready ? "ok" : "off"} label={p.cloud.enabled && p.cloud.ready ? "local + cloud" : "local"}
        title={p.cloud.enabled ? (p.cloud.ready ? `Cloud: ${p.cloud.provider} ${p.cloud.model}` : "Cloud is on but not ready") : "Nothing leaves this server"} />
      <Dot tone={workerLate ? "crit" : lane.pending > 2 ? "warn" : "ok"} label="queue"
        title={workerLate ? "The ForgeAI worker has not reported for a while: ./forgesre doctor" : `Answers wait at most ${p.timeout_s} s for the model${p.thinking ? "; thinking is on" : ""}`}>
        {lane.running ? `busy ${lane.running.seconds} s` : "idle"}{lane.pending ? ` · ${lane.pending} waiting` : ""}
      </Dot>
      {t.status === "error" && t.error ? <span className="pulse-why">{t.error}</span> : null}
      {error ? <span className="pulse-why">{error}</span> : null}
    </div>
  );
}
