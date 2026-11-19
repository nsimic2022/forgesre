import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { AssetCard, InventoryPage as Page } from "../lib/api";
import { InventoryPage } from "./Inventory";

const rowA = { asset_id: "A1", number: 1, hostname: "esx-prod-03", ip: "10.10.20.13", type: "ESXi host", environment: "prod", site: "DC Beograd", row: "A", rack: "A07", customer: "", owner: "Compute", state: "down" as const, sources: ["Prometheus", "GLPI"], open: 1 };
const rowB = { ...rowA, asset_id: "A2", number: 2, hostname: "sw-acc-08", type: "Switch", state: "up" as const, sources: ["SNMP"], open: 0 };
const page: Page = {
  library: {
    sites: [{ name: "DC Beograd", count: 2, racks: [{ name: "A07", count: 2 }] }],
    types: [{ name: "ESXi host", count: 1 }, { name: "Switch", count: 1 }],
    environments: [{ name: "prod", count: 2 }],
    customers: [],
    views: [{ key: "down", name: "Down now", count: 1 }],
  },
  total: 2, all: 2, offset: 0, limit: 50, rows: [rowA, rowB],
};
const card: AssetCard = { ...rowA, monitoring_profile: "linux", contact_name: "Marko", owner_email: "", owner_phone: "", notes: "", glpi_id: "1182", zabbix_hostid: "", scrape_address: "10.10.20.13:9100", parent: "sw-acc-07", details: [{ key: "contract", label: "License / contract / SLA", value: "ProSupport to 2027" }], incidents: [{ number: "INC-0412", title: "HostDown", severity: "CRITICAL", status: "OPEN", started_at: null }], can_edit: true };

describe("Inventory", () => {
  let urls: string[];
  beforeEach(() => {
    urls = [];
    vi.stubGlobal("fetch", vi.fn(async (url: string) => {
      urls.push(url);
      const body = url.startsWith("/api/v1/inventory/") ? card : page;
      return new Response(JSON.stringify(body), { status: 200, headers: { "Content-Type": "application/json" } });
    }));
  });
  afterEach(() => (cleanup(), vi.unstubAllGlobals()));

  const renderPage = () => render(<MemoryRouter><InventoryPage /></MemoryRouter>);

  it("shows the library and the table, filters by rack", async () => {
    renderPage();
    expect(await screen.findByText("esx-prod-03")).toBeTruthy();
    expect(screen.getByText("Down now")).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: /A07/ }));
    await waitFor(() => expect(urls.some((u) => u.includes("rack=A07") && u.includes("site=DC+Beograd"))).toBe(true));
  });

  it("opens the asset card with contract, sources and incidents", async () => {
    renderPage();
    fireEvent.click(await screen.findByRole("button", { name: "esx-prod-03" }));
    expect(await screen.findByText("ProSupport to 2027")).toBeTruthy();
    expect(screen.getByText("HostDown")).toBeTruthy();
    expect(screen.getByText("sw-acc-07")).toBeTruthy();
    expect(screen.getByRole("link", { name: "Edit (classic)" })).toBeTruthy();
  });
});
