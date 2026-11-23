import { useEffect, useMemo, useRef, useState } from "react";
import { useNavigate } from "react-router-dom";
import { getJson } from "../lib/api";
import { NAV } from "./Shell";

type Hit = { key: string; label: string; hint: string; to: string };
type Found = { incidents: { number: string; title: string; severity: string; status: string }[]; assets: { asset_id: string; hostname: string; ip: string; type: string }[] };

// A function, not a constant: Shell imports this file, so NAV is not ready at load time.
export const pages = (): Hit[] => [
  ...NAV.map((n) => ({ key: `p:${n.to}`, label: n.label, hint: "Page", to: n.to })),
  { key: "p:oncall", label: "On-call", hint: "Page", to: "/incidents/on-call" },
  { key: "p:add", label: "Add devices", hint: "Inventory", to: "/inventory/add" },
  { key: "p:map", label: "Dependency map", hint: "Inventory", to: "/inventory/map" },
  { key: "p:racks", label: "Racks", hint: "Inventory", to: "/inventory/racks" },
  { key: "p:ask", label: "Ask ForgeAI", hint: "Investigations", to: "/investigations#ask-forgeai" },
  { key: "p:tv", label: "Wall screen (TV mode)", hint: "Page", to: "/tv" },
  { key: "p:checks", label: "Checks and synthetic checks", hint: "Settings", to: "/settings?group=checks" },
];

/** True for Ctrl+K / Cmd+K, the usual "jump anywhere" key. */
export function isPaletteKey(e: KeyboardEvent): boolean {
  return (e.ctrlKey || e.metaKey) && !e.altKey && e.key.toLowerCase() === "k";
}

/**
 * Ctrl+K (Cmd+K on a Mac): type to jump to a page, an incident (number or title) or an asset
 * (host, id or IP). Arrow keys move, Enter opens, Escape closes. Search goes to /api/v1/search.
 */
export function CommandPalette() {
  const navigate = useNavigate();
  const [open, setOpen] = useState(false);
  const [q, setQ] = useState("");
  const [found, setFound] = useState<Found>({ incidents: [], assets: [] });
  const [pick, setPick] = useState(0);
  const input = useRef<HTMLInputElement>(null);

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (isPaletteKey(e)) {
        e.preventDefault();
        setOpen((o) => !o);
      }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, []);
  useEffect(() => {
    if (open) {
      setQ("");
      setPick(0);
      window.setTimeout(() => input.current?.focus(), 0);
    }
  }, [open]);
  useEffect(() => {
    if (!open || q.trim().length < 2) return setFound({ incidents: [], assets: [] });
    const t = window.setTimeout(() => {
      getJson<Found>(`/api/v1/search?q=${encodeURIComponent(q.trim())}`)
        .then((f) => setFound({ incidents: Array.isArray(f?.incidents) ? f.incidents : [], assets: Array.isArray(f?.assets) ? f.assets : [] }))
        .catch(() => setFound({ incidents: [], assets: [] }));
    }, 200);
    return () => window.clearTimeout(t);
  }, [q, open]);

  const hits = useMemo<Hit[]>(() => {
    const needle = q.trim().toLowerCase();
    const all = pages();
    const shown = all.filter((p) => !needle || p.label.toLowerCase().includes(needle) || p.hint.toLowerCase().includes(needle));
    return [
      ...shown.slice(0, needle ? 6 : all.length),
      ...found.incidents.map((i) => ({ key: `i:${i.number}`, label: `${i.number.split("_")[0]} ${i.title}`, hint: `${i.severity} · ${i.status}`, to: `/incidents/${encodeURIComponent(i.number)}` })),
      ...found.assets.map((a) => ({ key: `a:${a.asset_id}`, label: a.hostname || a.asset_id, hint: [a.type, a.ip].filter(Boolean).join(" · "), to: `/inventory?q=${encodeURIComponent(a.hostname || a.asset_id)}` })),
    ];
  }, [q, found]);
  useEffect(() => setPick((p) => Math.min(p, Math.max(0, hits.length - 1))), [hits.length]);

  if (!open) return null;
  const go = (hit: Hit | undefined) => {
    if (!hit) return;
    setOpen(false);
    navigate(hit.to);
  };
  return (
    <div className="shortcuts-backdrop" role="presentation" onClick={() => setOpen(false)}>
      <div className="card palette" role="dialog" aria-modal="true" aria-label="Go to" onClick={(e) => e.stopPropagation()}>
        <input ref={input} className="search" role="combobox" aria-expanded="true" aria-controls="palette-list" aria-label="Go to page, incident or asset"
          placeholder="Page, incident number or title, host or IP…" value={q}
          onChange={(e) => (setQ(e.target.value), setPick(0))}
          onKeyDown={(e) => {
            if (e.key === "Escape") setOpen(false);
            else if (e.key === "ArrowDown") (e.preventDefault(), setPick((p) => Math.min(p + 1, hits.length - 1)));
            else if (e.key === "ArrowUp") (e.preventDefault(), setPick((p) => Math.max(p - 1, 0)));
            else if (e.key === "Enter") (e.preventDefault(), go(hits[pick]));
          }} />
        <ul id="palette-list" role="listbox" className="palette-list">
          {hits.map((h, i) => (
            <li key={h.key} role="option" aria-selected={i === pick} className={i === pick ? "on" : ""} onMouseEnter={() => setPick(i)} onClick={() => go(h)}>
              <span>{h.label}</span><span className="small faint">{h.hint}</span>
            </li>
          ))}
          {hits.length === 0 ? <li className="small muted">Nothing matches.</li> : null}
        </ul>
        <span className="small faint">↑ ↓ to move · Enter to open · Esc to close</span>
      </div>
    </div>
  );
}
