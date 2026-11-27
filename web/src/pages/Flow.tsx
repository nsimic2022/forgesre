import { useCallback, useEffect, useRef, useState } from "react";
import { Link, useParams, useSearchParams } from "react-router-dom";
import { TopicChat } from "../components/ForgeAiChat";
import { Pager, usePageSize } from "../components/Pager";
import { Card, Chip } from "../components/ui";
import { getJson, postJson } from "../lib/api";
import { clock } from "../lib/format";

export type FlowStep = {
  id: string; label: string; sub: string; status: "ok" | "warn" | "bad" | "unknown" | "off"; detail: string; fix: string; ms?: number;
  log?: { direction?: string; kinds?: string[] }; rows?: { at: string; status: string; error: string; retries?: number; subject?: string }[];
};
type Lane = { id: string; title: string; steps: FlowStep[] };
type Ai = { status: string; at: string | null; question: string; answer: string; model?: string; call_id?: number | null };
type Flow = { name: string; label: string; about: string; configured: boolean; status: string; job?: number; error?: string;
  result: { checked_at: string; lanes: Lane[] } | null; watch?: boolean; ai?: Ai | null; target?: string; target_label?: string };
type Exchange = { id: number; at: string; direction: string; kind: string; endpoint: string; peer: string; status: string; http_status: number; ms: number; summary: string };
type Exchanges = { total: number; rows: Exchange[]; last_id: number };

const TONE: Record<string, "ok" | "warn" | "crit" | "off"> = { ok: "ok", warn: "warn", bad: "crit", unknown: "off", off: "off" };
const WORD: Record<string, string> = { ok: "Works", warn: "Warning", bad: "Broken", unknown: "Not checked", off: "Off" };
const COLOR: Record<string, string> = { ok: "var(--ok)", warn: "var(--warn)", bad: "var(--crit)", unknown: "var(--faint)", off: "var(--faint)" };
const STALE_MS = 5 * 60 * 1000;

/** The link between two blocks, coloured by the test of the hop into the next block. */
function Hop({ next }: { next: FlowStep }) {
  const dashed = next.status === "unknown" || next.status === "off";
  return (
    <div className="flow-hop" aria-hidden="true" title={`${next.label}: ${WORD[next.status]}`}>
      <span style={{ borderTop: `2px ${dashed ? "dashed" : "solid"} ${COLOR[next.status]}` }} />
      <i style={{ borderLeftColor: COLOR[next.status] }} />
    </div>
  );
}

/** Exchange log for one block (or all), paged, refreshed every 10 s while the page is open. */
function ExchangeLog({ name, filter }: { name: string; filter?: FlowStep["log"] }) {
  const [pageSize, setPageSize] = usePageSize("flow-exchanges");
  const [offset, setOffset] = useState(0);
  const [data, setData] = useState<Exchanges | null>(null);
  const query = `direction=${filter?.direction ?? ""}&kinds=${(filter?.kinds ?? []).join(",")}`;
  useEffect(() => setOffset(0), [query]);
  useEffect(() => {
    const load = () => getJson<Exchanges>(`/api/v1/integrations/${name}/exchanges?${query}&limit=${pageSize}&offset=${offset}`).then(setData).catch(() => undefined);
    load();
    const timer = window.setInterval(load, 10000);
    return () => window.clearInterval(timer);
  }, [name, query, pageSize, offset]);
  if (!data) return <span className="muted">Loading the log…</span>;
  if (!data.total) return <span className="small muted">No calls recorded for this step yet.</span>;
  return (
    <>
      <div className="table-wrap">
        <table className="data" aria-label="Exchange log">
          <thead><tr><th style={{ width: 130 }}>Time</th><th style={{ width: 120 }}>Way</th><th style={{ width: 140 }}>Call</th><th style={{ width: 110 }}>Result</th><th style={{ width: 70 }}>Time</th><th>From / to · what happened</th></tr></thead>
          <tbody>
            {data.rows.map((r) => (
              <tr key={r.id}>
                <td className="mono small">{clock(r.at)}</td>
                <td style={{ whiteSpace: "nowrap" }}>{r.direction === "in" ? "Zabbix → us" : "us → Zabbix"}</td>
                <td className="mono small">{r.kind}</td>
                <td><Chip tone={r.status === "ok" || r.status === "test" ? "ok" : r.status === "refused" ? "crit" : "warn"}>{r.status}{r.http_status ? ` ${r.http_status}` : ""}</Chip></td>
                <td className="small">{r.ms} ms</td>
                <td className="small"><span className="faint">{r.peer}</span> {r.summary}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      <Pager total={data.total} offset={offset} size={pageSize} onOffset={setOffset} onSize={(n) => (setPageSize(n), setOffset(0))} label="calls" />
    </>
  );
}

/** Container log lines that belong to this flow (docker-ro, masked), loaded on request. */
function FlowLog({ name, label }: { name: string; label: string }) {
  const [data, setData] = useState<{ lines: string[]; error: string } | null>(null);
  const [busy, setBusy] = useState(false);
  const load = () => (setBusy(true), getJson<{ lines: string[]; error: string }>(`/api/v1/integrations/${name}/logs`).then(setData)
    .catch((e: Error) => setData({ lines: [], error: e.message })).finally(() => setBusy(false)));
  return (
    <div className="cell-stack" style={{ gap: 6 }}>
      <div className="row" style={{ justifyContent: "space-between" }}>
        <h3 className="small faint" style={{ margin: 0, textTransform: "uppercase", letterSpacing: ".05em" }}>Container logs · {label}</h3>
        <button className="btn" type="button" onClick={() => void load()} disabled={busy}>{data ? "Refresh" : "Show"}</button>
      </div>
      {data?.error ? <span className="small" style={{ color: "var(--crit)" }}>{data.error}</span> : null}
      {data && !data.error && !data.lines.length ? <span className="small muted">No log line about {label} in the last lines.</span> : null}
      {data?.lines.length ? <pre className="log-box" aria-label={`Log lines about ${label}`}>{data.lines.join("\n")}</pre> : null}
      <span className="small faint">ForgeAI below reads the last 30 of these lines too. Passwords and tokens are masked.</span>
    </div>
  );
}

/** Wait for a call from the Zabbix server: shows the command, then what arrived. */
const TEST: Record<string, { path: string; sender: string; button: string }> = {
  zabbix: { path: "/api/v1/webhooks/zabbix", sender: "the Zabbix server", button: "or press Test on the ForgeSRE media type (Alerts › Media types)" },
  prometheus: { path: "/api/v1/webhooks/alertmanager", sender: "this server (where Alertmanager runs)", button: "or wait for the next alarm" },
};

function WaitForTest({ name, port }: { name: string; port: string }) {
  const test = TEST[name];
  const [after, setAfter] = useState<number | null>(null);
  const [got, setGot] = useState<Exchange | null>(null);
  const [error, setError] = useState("");
  useEffect(() => {
    if (after === null || got) return;
    const timer = window.setInterval(() => {
      getJson<Exchanges>(`/api/v1/integrations/${name}/exchanges?direction=in&after=${after}&limit=1`)
        .then((d) => (d.rows[0] ? setGot(d.rows[0]) : undefined)).catch((e: Error) => setError(e.message));
    }, 3000);
    return () => window.clearInterval(timer);
  }, [name, after, got]);
  const start = () => getJson<Exchanges>(`/api/v1/integrations/${name}/exchanges?limit=1`).then((d) => (setGot(null), setAfter(d.last_id))).catch((e: Error) => setError(e.message));
  return (
    <div className="flow-test">
      <strong className="small">Test call</strong>
      <span className="small muted">On {test.sender} run <span className="mono">curl -sS -X POST http://&lt;FORGE-IP&gt;:{port}{test.path}</span>, {test.button}. Any call that arrives shows here; a test never opens an incident by itself.</span>
      {after === null || got ? <button className="btn" type="button" onClick={() => void start()}>{got ? "Wait for another" : "Wait for a test"}</button> : <span className="small" role="status">Waiting for a call…</span>}
      {got ? <span className="small" role="status"><Chip tone={got.status === "ok" || got.status === "test" ? "ok" : "crit"}>{got.status} {got.http_status}</Chip> from {got.peer || "?"} at {clock(got.at)}: {got.summary}</span> : null}
      {error ? <span className="small" style={{ color: "var(--crit)" }}>{error}</span> : null}
    </div>
  );
}

/**
 * Settings › Integrations › <integration> › Flow: both directions as rows of blocks, each link
 * coloured by its test; a click opens the block's detail and its own log underneath; ForgeAI below
 * reads only this flow. Everything is read-only; the check runs as a background job.
 */
export function FlowPage() {
  const { name = "zabbix" } = useParams();
  const [search] = useSearchParams();
  const target = search.get("target") ?? "";
  // one device (SNMP, syslog): ?target=<asset id> checks only that object
  const q = target ? `?target=${encodeURIComponent(target)}` : "";
  const [flow, setFlow] = useState<Flow | null>(null);
  const [error, setError] = useState("");
  const [picked, setPicked] = useState("");
  const asked = useRef("");
  const load = useCallback(() => getJson<Flow>(`/api/v1/integrations/${name}/flow` + q).then(setFlow).catch((e: Error) => setError(e.message)), [name, q]);
  const run = () => postJson(`/api/v1/integrations/${name}/flow` + q, {}).then(load).catch((e: Error) => setError(e.message));
  useEffect(() => void load(), [load]);
  const running = flow?.status === "pending" || flow?.status === "running";
  useEffect(() => {
    if (!flow) return;
    const old = !flow.result || Date.now() - Date.parse(flow.result.checked_at) > STALE_MS;
    if (old && !running && asked.current !== name + q) {
      asked.current = name + q; // check once on open when the last result is missing or old
      void run();
    }
    if (running) {
      const timer = window.setTimeout(() => void load(), 1500);
      return () => window.clearTimeout(timer);
    }
  });
  const lanes = flow?.result?.lanes ?? [];
  const all = lanes.flatMap((l) => l.steps.map((s) => ({ ...s, key: `${l.id}:${s.id}` })));
  const firstBad = all.find((s) => s.status === "bad") ?? all.find((s) => s.status === "warn") ?? all[0];
  const current = all.find((s) => s.key === picked) ?? firstBad;
  const port = window.location.port || "8080";
  return (
    <>
      <header className="page-head">
        <div className="cell-stack" style={{ gap: 6 }}>
          <span className="small faint"><Link to="/settings?group=integrations">Settings › Integrations</Link> / {target
            ? <><Link to={`/settings/integrations/${name}`}>{flow?.label ?? name}</Link> / {flow?.target_label ?? target}</> : flow?.label ?? name}</span>
          <h1>{flow?.label ?? name} flow{target ? ` · ${flow?.target_label ?? target}` : ""}</h1>
          <span className="muted">
            {flow?.about ?? ""} Read-only.
            {flow?.result ? ` Checked ${clock(flow.result.checked_at)}.` : ""}
            {flow ? (flow.watch ? " Checked again every 15 minutes; a block that breaks shows in the journal and ForgeAI analyses it." : " Automatic checks are off (flows.watch).") : ""}
          </span>
        </div>
        <button className="btn primary" type="button" onClick={() => void run()} disabled={running}>{running ? "Checking…" : "Check again"}</button>
      </header>
      {error ? <div className="banner error" role="alert">{error}</div> : null}
      {flow?.error ? <div className="banner error" role="alert">The check failed: {flow.error}</div> : null}
      {!flow?.result ? <Card><span className="muted">{running || !flow ? "Checking the flow…" : "No check yet."}</span></Card> : null}
      {lanes.map((lane) => (
        <Card key={lane.id} title={lane.title}>
          <div className="flow-row" role="list" aria-label={lane.title}>
            {lane.steps.map((s, i) => {
              const key = `${lane.id}:${s.id}`;
              return (
                <div key={key} className="flow-cell" role="listitem">
                  {i ? <Hop next={s} /> : null}
                  <button type="button" className={`flow-block${current?.key === key ? " on" : ""} ${s.status}`} onClick={() => setPicked(key)}
                    aria-pressed={current?.key === key} aria-label={`${s.label}: ${WORD[s.status]}`}>
                    <span className="flow-name"><i style={{ background: COLOR[s.status] }} />{s.label}</span>
                    <span className="flow-sub">{s.sub}</span>
                    <span className="flow-sub">{WORD[s.status]}{s.ms !== undefined ? ` · ${s.ms} ms` : ""}</span>
                  </button>
                </div>
              );
            })}
          </div>
        </Card>
      ))}
      {current ? (
        <Card title={current.label} actions={<Chip tone={TONE[current.status]}>{WORD[current.status]}</Chip>}>
          <span>{current.detail}</span>
          {current.fix && current.status !== "ok" ? <div className="flow-fix"><strong className="small">What to do</strong><span className="mono small">{current.fix}</span></div> : null}
          {current.rows?.length ? (
            <div className="table-wrap">
              <table className="data" aria-label="Details of this step">
                <thead><tr><th style={{ width: 130 }}>Time</th><th style={{ width: 110 }}>Result</th><th style={{ width: 70 }}>Retries</th><th>Error · what</th></tr></thead>
                <tbody>
                  {current.rows.map((r, i) => (
                    <tr key={i}>
                      <td className="mono small">{r.at ? clock(r.at) : ""}</td>
                      <td><Chip tone={r.status === "sent" || r.status === "up" ? "ok" : r.status === "failed" || r.status === "down" || r.status === "err" ? "crit" : "warn"}>{r.status}</Chip></td>
                      <td className="small">{r.retries ?? ""}</td>
                      <td className="small">{r.subject ? <span className="faint">{r.subject} </span> : null}{r.error}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          ) : null}
          {(current.id === "network" || current.id === "webhook") && TEST[name] ? <WaitForTest name={name} port={port} /> : null}
          <h3 className="small faint" style={{ margin: "8px 0 0", textTransform: "uppercase", letterSpacing: ".05em" }}>Log of this step</h3>
          <ExchangeLog name={name} filter={current.log} />
          <FlowLog name={name} label={flow?.label ?? name} />
        </Card>
      ) : null}
      {flow?.ai ? (
        <Card title="ForgeAI analysis of the last break" actions={<span className="small faint">{clock(flow.ai.at)}{flow.ai.model ? ` · ${flow.ai.model}` : ""}</span>}>
          <span className="small faint">{flow.ai.question}</span>
          {flow.ai.answer ? <div style={{ whiteSpace: "pre-wrap" }}>{flow.ai.answer}</div>
            : <span className="muted">{flow.ai.status === "error" ? "The analysis failed; see Investigations." : "ForgeAI is analysing…"}</span>}
        </Card>
      ) : null}
      <Card title="Ask ForgeAI about this flow">
        <span className="small faint">ForgeAI reads only this page: the last check of every block and the recent calls. No incident data, no tokens.</span>
        <TopicChat topic={`flow:${name}`} />
      </Card>
    </>
  );
}
