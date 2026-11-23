import { useEffect, useMemo, useState } from "react";
import { getJson } from "../lib/api";
import { Card, Dot } from "./ui";

export type TreeNode = {
  kind: "root" | "site" | "row" | "rack" | "asset"; name: string; id?: string; type?: string; ip?: string; state?: string;
  open?: number; assets: number; open_total: number; worst: string; children: TreeNode[];
};

const TONE: Record<string, "crit" | "high" | "off" | "ok"> = { down: "crit", alarm: "high", unknown: "off", up: "ok" };
const KIND: Record<string, string> = { site: "Site", row: "Row", rack: "Rack" };

/** Keep nodes whose name, type or IP matches (and the path to them); everything when the filter is empty. */
function filterTree(node: TreeNode, q: string): TreeNode | null {
  if (!q) return node;
  const hit = [node.name, node.type, node.ip].some((v) => (v ?? "").toLowerCase().includes(q));
  const kids = node.children.map((c) => filterTree(c, q)).filter((c): c is TreeNode => c !== null);
  return hit || kids.length ? { ...node, children: hit && node.kind === "asset" ? node.children : kids } : null;
}

function Branch({ node, depth, open, toggle, selected, onPick, path }: {
  node: TreeNode; depth: number; open: Set<string>; toggle: (p: string) => void; selected: string; onPick: (id: string) => void; path: string;
}) {
  const key = `${path}/${node.kind}:${node.id ?? node.name}`;
  const expanded = open.has(key);
  const hasKids = node.children.length > 0;
  const isAsset = node.kind === "asset";
  return (
    <li role="treeitem" aria-expanded={hasKids ? expanded : undefined} aria-selected={isAsset ? selected === node.id : undefined}>
      <div className={`tree-row${isAsset && selected === node.id ? " on" : ""}`} style={{ paddingLeft: depth * 18 }}>
        {hasKids ? <button type="button" className="tree-twist" aria-label={expanded ? `Close ${node.name}` : `Open ${node.name}`} onClick={() => toggle(key)}>{expanded ? "▾" : "▸"}</button> : <span className="tree-twist" />}
        <Dot tone={TONE[isAsset ? node.state ?? "unknown" : node.worst] ?? "off"} />
        {isAsset ? (
          <button type="button" className="tree-name" onClick={() => onPick(node.id!)}>
            <strong>{node.name}</strong> <span className="small muted">{node.type}{node.ip ? ` · ${node.ip}` : ""}</span>
            {node.open ? <span className="small" style={{ color: "var(--high)" }}> · {node.open} open</span> : null}
          </button>
        ) : (
          <button type="button" className="tree-name" onClick={() => toggle(key)}>
            <span className="small faint">{KIND[node.kind] ?? ""}</span> <strong>{node.name}</strong>{" "}
            <span className="small muted">{node.assets} asset{node.assets === 1 ? "" : "s"}{node.open_total ? ` · ${node.open_total} open` : ""}</span>
          </button>
        )}
      </div>
      {hasKids && expanded ? (
        <ul role="group">
          {node.children.map((c) => <Branch key={`${c.kind}:${c.id ?? c.name}`} node={c} depth={depth + 1} open={open} toggle={toggle} selected={selected} onPick={onPick} path={key} />)}
        </ul>
      ) : null}
    </li>
  );
}

/**
 * Inventory › Tree: site → row → rack → asset, with children (VMs, blades) under their parent.
 * Each node shows its worst state, so a red rack stands out closed; searching opens the path.
 */
export function InventoryTree({ query, selected, onPick, reload }: { query: string; selected: string; onPick: (id: string) => void; reload: number }) {
  const [tree, setTree] = useState<TreeNode | null>(null);
  const [error, setError] = useState("");
  const [open, setOpen] = useState<Set<string>>(new Set());
  useEffect(() => void getJson<TreeNode>("/api/v1/inventory/tree").then(setTree).catch((e: Error) => setError(e.message)), [reload]);
  const q = query.trim().toLowerCase();
  const shown = useMemo(() => (tree ? filterTree(tree, q) : null), [tree, q]);
  // Open sites at first; open everything on the way to a search hit.
  useEffect(() => {
    if (!shown) return;
    const keys = new Set<string>();
    const walk = (n: TreeNode, path: string, depth: number) => {
      const key = `${path}/${n.kind}:${n.id ?? n.name}`;
      if ((n.kind !== "asset" && (q || depth < 1 || n.worst === "down")) || (q && n.children.length)) keys.add(key);
      n.children.forEach((c) => walk(c, key, depth + 1));
    };
    shown.children.forEach((c) => walk(c, "", 0));
    setOpen(keys);
  }, [shown, q]);
  const toggle = (k: string) => setOpen((s) => {
    const next = new Set(s);
    if (next.has(k)) next.delete(k); else next.add(k);
    return next;
  });
  return (
    <Card title="By location" actions={tree ? <span className="small faint">{tree.assets} assets · red = something down inside</span> : null}>
      {error ? <span className="small" role="alert" style={{ color: "var(--crit)" }}>{error}</span> : null}
      {!tree && !error ? <span className="muted">Loading…</span> : null}
      {shown && shown.children.length ? (
        <ul className="tree" role="tree" aria-label="Inventory by location">
          {shown.children.map((c) => <Branch key={`${c.kind}:${c.name}`} node={c} depth={0} open={open} toggle={toggle} selected={selected} onPick={onPick} path="" />)}
        </ul>
      ) : tree ? <span className="muted">Nothing matches.</span> : null}
      <span className="small faint">Location comes from each asset&apos;s site, row and rack; a parent (hypervisor, chassis, switch) holds its children.</span>
    </Card>
  );
}
