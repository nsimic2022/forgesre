import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { afterEach, describe, expect, it, vi } from "vitest";
import type { SettingRow } from "../lib/api";
import { IntegrationsPanel, type IntegrationCard } from "./IntegrationsPanel";

const zabbix: IntegrationCard = {
  id: "zabbix", label: "Zabbix", role: "Reads hosts.", kind: "source", status: "error", why: "HTTP 401", links: [{ label: "GUI", url: "https://zabbix.dc.local" }],
  local_only: false, local_hint: "", settings: ["inventory.zabbix.url", "ZABBIX_API_TOKEN"], test: "zabbix", sync: "zabbix",
  last_sync: { at: "2026-11-20T06:00:00Z", status: "error", summary: "Zabbix sync failed" },
};
const prom: IntegrationCard = { ...zabbix, id: "prometheus", label: "Prometheus", kind: "stack", status: "ok", why: "", links: [], local_only: true,
  local_hint: "Only on the appliance: ssh -L 9090:127.0.0.1:9090", settings: [], test: "prometheus", sync: "", last_sync: null };
const rows: SettingRow[] = [
  { key: "inventory.zabbix.url", group: "integrations", label: "Zabbix URL", kind: "url", store: "yaml", editable: true, help: "", test: "zabbix", value: "https://zabbix.dc.local", effective: null },
  { key: "ZABBIX_API_TOKEN", group: "integrations", label: "Zabbix API token", kind: "str", store: "secret", editable: false, help: "", test: "", value: null, effective: null, set: true },
];

describe("IntegrationsPanel", () => {
  afterEach(() => (cleanup(), vi.unstubAllGlobals()));

  it("shows status, the web link, last sync, settings in place and queues a sync", async () => {
    const calls: string[] = [];
    vi.stubGlobal("fetch", vi.fn(async (url: string, init?: RequestInit) => {
      calls.push(`${init?.method ?? "GET"} ${url}`);
      const body = url === "/api/v1/integrations" ? { sources: [zabbix], stack: [prom] } : { queued: true };
      return new Response(JSON.stringify(body), { status: 200, headers: { "Content-Type": "application/json" } });
    }));
    render(<MemoryRouter><IntegrationsPanel rows={rows} onSaved={() => undefined} /></MemoryRouter>);
    const link = await screen.findByRole("link", { name: "Open GUI" });
    expect(link.getAttribute("href")).toBe("https://zabbix.dc.local");
    expect(link.getAttribute("rel")).toContain("noopener");
    expect(screen.getByText("HTTP 401")).toBeTruthy();
    expect(screen.getByText(/Zabbix sync failed/)).toBeTruthy();
    expect(screen.getByText("Only on the appliance: ssh -L 9090:127.0.0.1:9090")).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "Settings (2)" }));
    expect(screen.getByLabelText(/Zabbix URL/)).toBeTruthy();
    expect(screen.getByText("Set")).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "Sync now" }));
    await waitFor(() => expect(calls).toContain("POST /api/v1/integrations/zabbix/sync"));
    expect(await screen.findByRole("button", { name: "Sync queued" })).toBeTruthy();
  });

  it("does not repeat the card's reason after Test connection", async () => {
    vi.stubGlobal("fetch", vi.fn(async (url: string) => {
      const body = url === "/api/v1/integrations" ? { sources: [zabbix], stack: [] } : { status: "error", why: "HTTP 401" };
      return new Response(JSON.stringify(body), { status: 200, headers: { "Content-Type": "application/json" } });
    }));
    render(<MemoryRouter><IntegrationsPanel rows={rows} onSaved={() => undefined} /></MemoryRouter>);
    fireEvent.click(await screen.findByRole("button", { name: "Test connection" }));
    await waitFor(() => expect(screen.getAllByText("error")).toHaveLength(2));
    expect(screen.getAllByText("HTTP 401")).toHaveLength(1);
  });

  it("links the Zabbix card to its Flow page", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => new Response(JSON.stringify({ sources: [{ ...zabbix, flow: "/settings/integrations/zabbix" }], stack: [] }),
      { status: 200, headers: { "Content-Type": "application/json" } })));
    render(<MemoryRouter><IntegrationsPanel rows={rows} onSaved={() => undefined} /></MemoryRouter>);
    const link = await screen.findByRole("link", { name: "Flow and log" });
    expect(link.getAttribute("href")).toBe("/settings/integrations/zabbix");
    expect(screen.getByRole("link", { name: "Integration map" }).getAttribute("href")).toBe("/settings/flows");
  });
});
