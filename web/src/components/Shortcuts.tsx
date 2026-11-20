import { useEffect, useState } from "react";
import { useLocation, useNavigate } from "react-router-dom";
import { NAV } from "./Shell";

// "g" then a letter jumps to a page, like GitHub or Gmail. Keys are ignored while typing.
export const GO: Record<string, string> = { n: "/", i: "/incidents", o: "/incidents/on-call", v: "/inventory", a: "/automation", t: "/investigations", r: "/reports", s: "/settings", w: "/tv" };

function typing(target: EventTarget | null): boolean {
  const el = target as HTMLElement | null;
  if (!el) return false;
  return el.isContentEditable || ["INPUT", "TEXTAREA", "SELECT"].includes(el.tagName);
}

/** Page title in the browser tab: "Inventory · ForgeSRE" (incident pages use the number). */
export function titleFor(path: string): string {
  if (path.startsWith("/incidents/") && path !== "/incidents/on-call") return `${decodeURIComponent(path.split("/")[2] ?? "").split("_")[0]} · ForgeSRE`;
  const special: Record<string, string> = { "/incidents/on-call": "On-call", "/inventory/add": "Add devices", "/inventory/map": "Dependency map" };
  const item = special[path] ?? NAV.find((n) => n.to === path)?.label;
  return item ? `${item} · ForgeSRE` : "ForgeSRE";
}

/**
 * Keyboard shortcuts for the whole app: "/" focuses the page's search box, "g" + letter opens a
 * page, "?" shows the list, Escape closes it. Also keeps the browser tab title in step with the page.
 */
export function Shortcuts() {
  const navigate = useNavigate();
  const location = useLocation();
  const [help, setHelp] = useState(false);
  useEffect(() => {
    document.title = titleFor(location.pathname);
  }, [location.pathname]);
  useEffect(() => {
    let pendingG = 0;
    const onKey = (e: KeyboardEvent) => {
      if (e.ctrlKey || e.metaKey || e.altKey) return;
      if (e.key === "Escape") return setHelp(false);
      if (typing(e.target)) return;
      if (e.key === "?") return (e.preventDefault(), setHelp((h) => !h));
      if (e.key === "/") {
        const box = document.querySelector<HTMLInputElement>("main input[type=search], main input.search");
        if (box) (e.preventDefault(), box.focus(), box.select());
        return;
      }
      if (e.key === "g") return void (pendingG = Date.now());
      if (pendingG && Date.now() - pendingG < 1500 && GO[e.key]) {
        pendingG = 0;
        e.preventDefault();
        navigate(GO[e.key]);
      }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [navigate]);
  if (!help) return null;
  return (
    <div className="shortcuts-backdrop" role="presentation" onClick={() => setHelp(false)}>
      <div className="card shortcuts" role="dialog" aria-modal="true" aria-label="Keyboard shortcuts" onClick={(e) => e.stopPropagation()}>
        <div className="row" style={{ justifyContent: "space-between" }}>
          <strong>Keyboard shortcuts</strong>
          <button className="btn" type="button" onClick={() => setHelp(false)} autoFocus>Close</button>
        </div>
        <table className="data">
          <tbody>
            <tr><td><kbd>/</kbd></td><td>Search on this page</td></tr>
            <tr><td><kbd>?</kbd></td><td>Show or hide this list</td></tr>
            {[["n", "NOC overview"], ["i", "Incidents"], ["o", "On-call"], ["v", "Inventory"], ["a", "Automation"], ["t", "Investigations"], ["r", "Reports"], ["s", "Settings"], ["w", "Wall screen (TV mode)"]].map(([k, label]) => (
              <tr key={k}><td><kbd>g</kbd> then <kbd>{k}</kbd></td><td>{label}</td></tr>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  );
}
