import { useEffect, useState } from "react";
import { getJson } from "../lib/api";
import { Card, Chip } from "./ui";

type Top = { asset: string; interface: string; alias: string; bps: number };
type Bgp = { asset: string; peer: string; remote_as: number; state: string; enabled: boolean; established_s: number; ok: boolean };
type Ospf = { asset: string; neighbour: string; state: string; ok: boolean };
export type NetworkData = { top_in: Top[]; top_out: Top[]; bgp: Bgp[]; ospf: Ospf[]; error: string };

const list = <T,>(v: T[] | undefined) => (Array.isArray(v) ? v : []);

export function bits(bps: number): string {
  for (const [unit, size] of [["Gb/s", 1e9], ["Mb/s", 1e6], ["kb/s", 1e3]] as const) if (bps >= size) return `${(bps / size).toFixed(1)} ${unit}`;
  return `${Math.round(bps)} b/s`;
}

/** NOC "Network": BGP / OSPF sessions that are not up, then the busiest interfaces. Hidden without SNMP data. */
export function NetworkPanel() {
  const [data, setData] = useState<NetworkData | null>(null);
  useEffect(() => {
    const load = () => getJson<NetworkData>("/api/v1/network").then(setData).catch(() => setData(null));
    void load();
    const t = window.setInterval(load, 60_000);
    return () => window.clearInterval(t);
  }, []);
  const bgp = list(data?.bgp), ospf = list(data?.ospf), tin = list(data?.top_in), tout = list(data?.top_out);
  if (!data || data.error || (!bgp.length && !ospf.length && !tin.length)) return null;
  const badBgp = bgp.filter((r) => !r.ok), badOspf = ospf.filter((r) => !r.ok);
  return (
    <Card title="Network" actions={<span className="small faint">{bgp.length} BGP · {ospf.length} OSPF</span>}>
      {bgp.length || ospf.length ? (
        <div className="row" style={{ gap: 8, flexWrap: "wrap" }} aria-label="Routing">
          {badBgp.length + badOspf.length === 0 ? <Chip tone="ok">All BGP and OSPF sessions up</Chip> : null}
          {badBgp.map((r) => <Chip key={`b-${r.asset}-${r.peer}`} tone="crit">BGP {r.asset} → {r.peer} (AS {r.remote_as}) {r.state}</Chip>)}
          {badOspf.map((r) => <Chip key={`o-${r.asset}-${r.neighbour}`} tone="warn">OSPF {r.asset} → {r.neighbour} {r.state}</Chip>)}
        </div>
      ) : null}
      {tin.length ? (
        <div className="cols" style={{ marginTop: 8 }}>
          {([["Busiest in", tin], ["Busiest out", tout]] as const).map(([title, rows]) => (
            <div key={title} style={{ flex: "1 1 280px", minWidth: 0 }}>
              <span className="small faint">{title} · 5 min</span>
              <table className="data">
                <tbody>
                  {rows.slice(0, 5).map((r) => (
                    <tr key={`${r.asset}-${r.interface}`}>
                      <td className="mono small">{r.asset}</td>
                      <td className="small" title={r.alias}>{r.interface}</td>
                      <td className="small" style={{ textAlign: "right" }}>{bits(r.bps)}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          ))}
        </div>
      ) : null}
    </Card>
  );
}
