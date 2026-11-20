import { useEffect, useState } from "react";
import { getJson, postJson } from "../lib/api";
import { clock } from "../lib/format";
import { Card, Chip } from "./ui";

type Source = { source: string; enabled: boolean; linked: number; conflicts: number; last: { at: string | null; status: string; summary: string } | null };
type Conflict = { id: number; source: string; source_id: string; kind: "field" | "ip" | "match"; field: string; ours: string; theirs: string; detail: string; asset_id: string; hostname: string; seen_at: string | null };
export type SourcesData = { sources: Source[]; conflicts: Conflict[]; can_edit: boolean; new_devices: string };

const NAMES: Record<string, string> = { glpi: "GLPI", zabbix: "Zabbix", netbox: "NetBox", librenms: "LibreNMS" };

/** Inventory › Sources: what each feeder last did and where it disagrees with the inventory.
 *  A disagreement is never written by a sync; an Engineer keeps ours or takes theirs here. */
export function SourcesPanel({ onClose, onChanged }: { onClose: () => void; onChanged: () => void }) {
  const [data, setData] = useState<SourcesData | null>(null);
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(0);
  const load = () => getJson<SourcesData>("/api/v1/inventory/sources").then(setData).catch((e: Error) => setError(e.message));
  useEffect(() => void load(), []);
  const decide = async (id: number, action: "ours" | "theirs") => {
    setBusy(id);
    setError("");
    try {
      await postJson(`/api/v1/inventory/conflicts/${id}`, { action });
      await load();
      onChanged();
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setBusy(0);
    }
  };
  return (
    <Card title="Sources" actions={<button className="btn" type="button" onClick={onClose}>Close</button>}>
      <span className="small muted">
        GLPI, Zabbix, NetBox and LibreNMS feed one inventory. A device is matched by its source id, then serial, MAC, name and IP.
        Empty fields are filled; a field a source owns (GLPI site, customer, contract) follows it until someone changes it here.
        Anything else that differs waits below and is never overwritten. New devices from sources: <strong>{data?.new_devices === "review" ? "listed in Discovery first" : "added to the inventory"}</strong> (Settings › Integrations).
      </span>
      {data ? (
        <div className="table-wrap">
          <table className="data">
            <thead><tr><th>Source</th><th>On</th><th>Linked</th><th>To review</th><th>Last sync</th></tr></thead>
            <tbody>
              {data.sources.map((s) => (
                <tr key={s.source}>
                  <td style={{ fontWeight: 600 }}>{NAMES[s.source] ?? s.source}</td>
                  <td>{s.enabled ? <Chip tone="ok">on</Chip> : <Chip tone="off">off</Chip>}</td>
                  <td>{s.linked}</td>
                  <td>{s.conflicts ? <Chip tone="warn">{s.conflicts}</Chip> : <span className="faint">0</span>}</td>
                  <td className="small">{s.last ? <>{clock(s.last.at)} · <span style={{ color: s.last.status === "error" ? "var(--crit)" : undefined }}>{s.last.summary}</span></> : <span className="faint">never</span>}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      ) : <span className="muted">Loading…</span>}
      {error ? <div className="banner error" role="alert">{error}</div> : null}
      {data && data.conflicts.length === 0 ? <span className="small muted">Every source agrees with the inventory.</span> : null}
      {data?.conflicts.map((c) => (
        <div key={c.id} className="cell-stack" style={{ borderTop: "1px solid var(--border)", paddingTop: 8, gap: 4 }}>
          <div className="row" style={{ justifyContent: "space-between" }}>
            <span><strong>{c.hostname || c.source_id}</strong> <span className="small faint">· {NAMES[c.source] ?? c.source}{c.field ? ` · ${c.field}` : ""}</span></span>
            <span className="small faint">{clock(c.seen_at)}</span>
          </div>
          {c.kind === "match" ? null : (
            <span className="small">Inventory: <span className="mono">{c.ours || "—"}</span> · {NAMES[c.source] ?? c.source}: <span className="mono">{c.theirs || "—"}</span></span>
          )}
          <span className="small muted">{c.detail}</span>
          {data.can_edit ? (
            <div className="row" style={{ gap: 6 }}>
              <button className="btn" type="button" disabled={busy === c.id} onClick={() => void decide(c.id, "ours")}>{c.kind === "match" ? "Understood" : "Keep inventory value"}</button>
              {c.kind !== "match" ? <button className="btn" type="button" disabled={busy === c.id} onClick={() => void decide(c.id, "theirs")}>Take {NAMES[c.source] ?? c.source} value</button> : null}
            </div>
          ) : null}
        </div>
      ))}
    </Card>
  );
}
