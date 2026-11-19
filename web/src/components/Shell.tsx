import type { ReactNode } from "react";
import { NavLink } from "react-router-dom";
import type { Me } from "../lib/api";
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

const ROLE_LABEL: Record<string, string> = { viewer: "Viewer", analyst: "Analyst", engineer: "Engineer", admin: "Admin" };

export function Shell({ me, forgeai, children }: { me: Me | null; forgeai: string; children: ReactNode }) {
  return (
    <div className="shell">
      <nav className="nav" aria-label="Main">
        <div className="nav-brand">
          <span className="nav-logo" aria-hidden="true">F</span>
          ForgeSRE <span className="small faint">2.0</span>
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
          <a className="small" href="/classic">Open classic UI</a>
        </div>
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
      <main className="main">{children}</main>
    </div>
  );
}
