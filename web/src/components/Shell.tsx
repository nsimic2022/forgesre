import { useEffect, useState, type ReactNode } from "react";
import { NavLink } from "react-router-dom";
import type { Me } from "../lib/api";
import { Shortcuts } from "./Shortcuts";
import { Chip } from "./ui";

// Main menu of the new UI, in the order of the approved designs.
export const NAV = [
  { to: "/", label: "NOC overview", end: true },
  { to: "/incidents", label: "Incidents" },
  { to: "/inventory", label: "Inventory" },
  { to: "/automation", label: "Automation" },
  { to: "/investigations", label: "Investigations" },
  { to: "/reports", label: "Reports" },
  { to: "/settings", label: "Settings" },
];

/** "2.0.0" → "2.0": the menu shows major.minor, like the release notes. */
export function shortVersion(v: string): string {
  return v.split(".").slice(0, 2).join(".");
}

const ROLE_LABEL: Record<string, string> = { viewer: "Viewer", analyst: "Analyst", engineer: "Engineer", admin: "Admin" };

const NAV_KEY = "forgesre.navClosed";

function savedClosed(): boolean {
  try {
    return localStorage.getItem(NAV_KEY) === "1";
  } catch {
    return false;
  }
}

/** The three-line menu button; ``open`` says what pressing it will do. */
function MenuButton({ closed, onClick, className }: { closed: boolean; onClick: () => void; className: string }) {
  return (
    <button type="button" className={className} onClick={onClick} aria-expanded={!closed} aria-controls="main-nav"
      aria-label={closed ? "Show the menu" : "Hide the menu"} title={closed ? "Show the menu" : "Hide the menu (page uses the full width)"}>
      <span aria-hidden="true" className="burger"><i /><i /><i /></span>
    </button>
  );
}

/**
 * App frame: the main menu on the left and the page. The menu scrolls on its own when the window is
 * short, and the ☰ button folds it away so the page takes the full width (remembered per browser).
 */
export function Shell({ me, forgeai, children }: { me: Me | null; forgeai: string; children: ReactNode }) {
  const [closed, setClosed] = useState(savedClosed);
  useEffect(() => {
    try {
      localStorage.setItem(NAV_KEY, closed ? "1" : "0");
    } catch {
      /* private mode: lasts until the tab closes */
    }
  }, [closed]);
  const toggle = () => setClosed((c) => !c);
  return (
    <div className={`shell${closed ? " nav-closed" : ""}`}>
      <a className="skip-link" href="#main">Skip to content</a>
      <Shortcuts />
      {closed ? <MenuButton closed className="nav-open" onClick={toggle} /> : null}
      <nav className="nav" id="main-nav" aria-label="Main" hidden={closed}>
        <div className="nav-brand">
          <MenuButton closed={false} className="nav-toggle" onClick={toggle} />
          <span className="nav-logo" aria-hidden="true">F/</span>
          ForgeSRE {me?.version ? <span className="small faint">{shortVersion(me.version)}</span> : null}
        </div>
        <div className="nav-links">
          {NAV.map((item) => (
            <NavLink key={item.to} to={item.to} end={item.end} className={({ isActive }) => `nav-link${isActive ? " active" : ""}`}>
              {item.label}
            </NavLink>
          ))}
        </div>
        <div className="nav-box">
          <div className="nav-row">
            <span>ForgeRCA</span>
            <Chip tone="ok">On</Chip>
          </div>
          <div className="nav-row">
            <span>ForgeAI</span>
            <Chip tone={forgeai === "off" ? "off" : "info"}>{forgeai === "off" ? "Off" : forgeai === "external" ? "External" : "Local"}</Chip>
          </div>
        </div>
        <span className="small faint nav-hint">Press <kbd>?</kbd> for keyboard shortcuts</span>
        {me ? (
          <div className="nav-user">
            <div style={{ display: "flex", flexDirection: "column", minWidth: 0 }}>
              <span style={{ overflow: "hidden", textOverflow: "ellipsis" }}>{me.name || me.email}</span>
              <span className="small faint">{ROLE_LABEL[me.role] ?? me.role}</span>
            </div>
            <form method="post" action="/logout">
              <button className="btn" type="submit">Sign out</button>
            </form>
          </div>
        ) : null}
      </nav>
      <main className="main" id="main" tabIndex={-1}>{children}</main>
    </div>
  );
}
