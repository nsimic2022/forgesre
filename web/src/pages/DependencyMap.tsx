import { useEffect, useMemo, useState } from "react";
import { Link } from "react-router-dom";
import { Card, Chip } from "../components/ui";
import { getJson, postJson, type TopoNode, type Topology } from "../lib/api";
import { clock } from "../lib/format";

function flatten(nodes: TopoNode[], out: TopoNode[] = []): TopoNode[] {
  for (const n of nodes) {
    out.push(n);
    flatten(n.children, out);
  }
  return out;
}

function ParentEditor({ node, all, onSaved }: { node: TopoNode; all: TopoNode[]; onSaved: () => void }) {
  const [value, setValue] = useState("");
  const [error, setError] = useState("");
  const save = (parent: string) => {
    const match = all.find((n) => n.hostname === parent || n.asset_id === parent || n.ip === parent);
    if (parent && !match) return setError("No asset with that name");
    postJson("/api/v1/topology/parent", { asset_id: node.asset_id, parent_asset_id: match?.asset_id ?? "" }).then(onSaved).catch((e: Error) => setError(e.message));
  };
  return (
    <span className="row" style={{ gap: 6 }}>
      <input className="search" style={{ minWidth: 160, width: 180 }} list="topo-assets" placeholder="parent hostname" aria-label={`Parent of ${node.hostname}`} value={value} onChange={(e) => setValue(e.target.value)} />
      <button className="btn" type="button" onClick={() => save(value.trim())}>Set</button>
      {error ? <span className="small" style={{ color: "var(--crit)" }}>{error}</span> : null}
    </span>
  );
}

function Branch({ node, depth, edit, all, onSaved }: { node: TopoNode; depth: number; edit: boolean; all: TopoNode[]; onSaved: () => void }) {
  const [open, setOpen] = useState(depth < 2);
  const [editing, setEditing] = useState(false);
  return (
    <li style={{ listStyle: "none" }}>
      <div className="row" style={{ gap: 8, padding: "6px 0", borderBottom: "1px solid var(--border)", paddingLeft: depth * 22 }}>
        {node.children.length ? (
          <button className="btn" type="button" style={{ minHeight: 26, padding: "0 8px" }} aria-expanded={open} aria-label={`${open ? "Collapse" : "Expand"} ${node.hostname}`} onClick={() => setOpen(!open)}>{open ? "−" : "+"}</button>
        ) : <span style={{ width: 30 }} />}
        <span aria-hidden="true" className="dot" style={{ background: node.alarm ? "var(--crit)" : "var(--ok)" }} />
        <Link to={`/inventory?q=${encodeURIComponent(node.hostname)}`} style={{ color: "var(--text)", fontWeight: 600 }}>{node.hostname}</Link>
        <span className="small faint">{node.type}{node.ip ? ` · ${node.ip}` : ""}</span>
        {node.alarm ? <Chip tone="crit">Alarm</Chip> : null}
        {node.behind ? <span className="small muted">{node.behind} behind it</span> : null}
        {edit ? (editing ? <ParentEditor node={node} all={all} onSaved={onSaved} /> : <button className="btn" type="button" style={{ marginLeft: "auto" }} onClick={() => setEditing(true)} aria-label={`Change parent of ${node.hostname}`}>Change parent</button>) : null}
      </div>
      {open && node.children.length ? (
        <ul style={{ margin: 0, padding: 0 }}>{node.children.map((c) => <Branch key={c.asset_id} node={c} depth={depth + 1} edit={edit} all={all} onSaved={onSaved} />)}</ul>
      ) : null}
    </li>
  );
}

/**
 * Inventory › Dependency map. A parent going down keeps everything behind it quiet (one incident,
 * not fifty), so this page shows that tree, proposes parents from LibreNMS LLDP/CDP links, and lets
 * an Engineer accept or change them.
 */
export function DependencyMapPage() {
  const [data, setData] = useState<Topology | null>(null);
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const [showAlone, setShowAlone] = useState(false);
  const load = () => getJson<Topology>("/api/v1/topology").then(setData).catch((e: Error) => setError(e.message));
  useEffect(() => {
    load();
  }, []);
  const all = useMemo(() => (data ? flatten([...data.roots, ...data.unlinked]) : []), [data]);
  const accept = (rows: Topology["suggestions"]) =>
    Promise.all(rows.map((s) => postJson("/api/v1/topology/parent", { asset_id: s.asset_id, parent_asset_id: s.parent_asset_id })))
      .then(() => (setNotice(`${rows.length} parent${rows.length === 1 ? "" : "s"} set.`), load()))
      .catch((e: Error) => setError(e.message));
  const sync = () => postJson("/api/v1/topology/sync", {}).then(() => setNotice("Reading links from LibreNMS in the background. Reload in a minute.")).catch((e: Error) => setError(e.message));
  const edit = !!data?.can_edit;
  return (
    <>
      <header className="page-head">
        <div className="cell-stack" style={{ gap: 6 }}>
          <span className="label">Inventory</span>
          <h1>Dependency map</h1>
          <span className="muted">{data ? `${data.with_parent} of ${data.assets} assets have a parent · ${data.links} LLDP/CDP links · last sync ${clock(data.last_sync)}` : "Loading…"}</span>
        </div>
        <div className="row">
          <Link className="btn" to="/inventory">Asset list</Link>
          {edit ? <button className="btn primary" type="button" onClick={sync}>Sync from LibreNMS</button> : null}
        </div>
      </header>
      <Card><span className="muted">When an asset is down, alarms from everything behind it are held back: you get one incident for the switch instead of one per server. The incident for each hidden asset still exists and opens if it is still failing after the parent is back.</span></Card>
      {error ? <div className="banner error" role="alert">{error}</div> : null}
      {notice ? <div className="banner" role="status" style={{ background: "var(--ok-bg)" }}>{notice}</div> : null}
      {data && data.suggestions.length ? (
        <Card title={`Suggested parents · ${data.suggestions.length}`} actions={edit ? <button className="btn primary" type="button" onClick={() => accept(data.suggestions)}>Accept all</button> : null}>
          <div className="table-wrap">
            <table className="data">
              <thead><tr><th>Asset</th><th>Proposed parent</th><th>Seen as</th><th>Now</th>{edit ? <th style={{ width: 110 }} /> : null}</tr></thead>
              <tbody>
                {data.suggestions.map((s) => (
                  <tr key={s.asset_id}>
                    <td style={{ fontWeight: 600 }}>{s.hostname}</td>
                    <td>{s.parent_hostname}</td>
                    <td className="small muted mono">{[s.protocol, s.port].filter(Boolean).join(" · ")}</td>
                    <td className="small faint">{s.current_parent || "no parent"}</td>
                    {edit ? <td><button className="btn" type="button" onClick={() => accept([s])} aria-label={`Accept ${s.parent_hostname} as parent of ${s.hostname}`}>Accept</button></td> : null}
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
          <span className="small faint">Only links where one end is a network device are proposed. Switch-to-switch links are not guessed: set those by hand.</span>
        </Card>
      ) : null}
      {data ? (
        <Card title="Tree" actions={<span className="small faint">Largest first · red = open alarm</span>}>
          {data.roots.length === 0 ? <div className="empty">No asset has a parent yet. Accept suggestions or set a parent below.</div> : (
            <ul style={{ margin: 0, padding: 0 }}>{data.roots.map((n) => <Branch key={n.asset_id} node={n} depth={0} edit={edit} all={all} onSaved={load} />)}</ul>
          )}
        </Card>
      ) : null}
      {data && data.unlinked.length ? (
        <Card title={`Without parent or children · ${data.unlinked.length}`} actions={<button className="btn" type="button" onClick={() => setShowAlone(!showAlone)} aria-expanded={showAlone}>{showAlone ? "Hide" : "Show"}</button>}>
          {showAlone ? <ul style={{ margin: 0, padding: 0 }}>{data.unlinked.map((n) => <Branch key={n.asset_id} node={n} depth={0} edit={edit} all={all} onSaved={load} />)}</ul> : <span className="small faint">These are alarmed on their own.</span>}
        </Card>
      ) : null}
      <datalist id="topo-assets">{all.map((n) => <option key={n.asset_id} value={n.hostname} />)}</datalist>
    </>
  );
}
