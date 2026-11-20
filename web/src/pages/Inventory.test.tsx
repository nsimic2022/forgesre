import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { AssetCard, AssetFormOptions, InventoryPage as Page } from "../lib/api";
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
  total: 2, all: 2, offset: 0, limit: 50, rows: [rowA, rowB], can_edit: true,
};
const options: AssetFormOptions = {
  types: [{ group: "Servers", names: ["Linux Server", "Hypervisor"] }, { group: "Network", names: ["Switch"] }],
  extra_fields: [{ key: "site", label: "Site / DC / room", max: 255 }, { key: "rack", label: "Rack", max: 64 }, { key: "contract", label: "License / contract / SLA", max: 500 }],
  environments: ["Production"], owners: ["Compute"], sites: ["DC Beograd"], can_edit: true,
};
const card: AssetCard = { ...rowA, monitoring_profile: "linux", contact_name: "Marko", owner_email: "", owner_phone: "", notes: "", glpi_id: "1182", zabbix_hostid: "", scrape_address: "10.10.20.13:9100", parent: "sw-acc-07", details: [{ key: "contract", label: "License / contract / SLA", value: "ProSupport to 2027" }], incidents: [{ number: "INC-0412", title: "HostDown", severity: "CRITICAL", status: "OPEN", started_at: null }], can_edit: true, extras: { site: "DC Beograd", rack: "A07", contract: "ProSupport to 2027", support: "yes", snmp_community_set: true } };

describe("Inventory", () => {
  let urls: string[];
  let posts: { url: string; body: Record<string, unknown> }[];
  beforeEach(() => {
    urls = [];
    posts = [];
    vi.stubGlobal("fetch", vi.fn(async (url: string, init?: RequestInit) => {
      urls.push(url);
      if (init?.method === "POST") {
        posts.push({ url, body: JSON.parse(String(init.body)) });
        const body = url.endsWith("/bulk-delete") ? { removed: ["A1"], refused: [] } : { asset_id: "A9" };
        return new Response(JSON.stringify(body), { status: 200, headers: { "Content-Type": "application/json" } });
      }
      const body = url === "/api/v1/inventory-form" ? options : url.startsWith("/api/v1/inventory/") ? card : page;
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
    expect(screen.getByRole("button", { name: "Edit" })).toBeTruthy();
  });

  it("adds an asset with location extras", async () => {
    renderPage();
    fireEvent.click(await screen.findByRole("button", { name: "New asset by hand" }));
    fireEvent.change(await screen.findByLabelText("Hostname"), { target: { value: "db-new-01" } });
    fireEvent.change(screen.getByLabelText("IP address"), { target: { value: "10.0.0.5" } });
    fireEvent.change(screen.getByLabelText("Type"), { target: { value: "Linux Server" } });
    fireEvent.change(screen.getByLabelText("Rack"), { target: { value: "B02" } });
    fireEvent.click(screen.getByRole("button", { name: "Add asset" }));
    await waitFor(() => expect(posts.length).toBe(1));
    expect(posts[0].url).toBe("/api/v1/assets");
    expect(posts[0].body).toMatchObject({ hostname: "db-new-01", ip: "10.0.0.5", type: "Linux Server", extras: { rack: "B02" } });
    expect(await screen.findByText("Asset added.")).toBeTruthy();
  });

  it("edits an asset and keeps saved extras the form does not show", async () => {
    renderPage();
    fireEvent.click(await screen.findByRole("button", { name: "esx-prod-03" }));
    fireEvent.click(await screen.findByRole("button", { name: "Edit" }));
    const rack = await screen.findByLabelText("Rack");
    expect((rack as HTMLInputElement).value).toBe("A07");
    fireEvent.change(rack, { target: { value: "A09" } });
    fireEvent.click(screen.getByRole("button", { name: "Save changes" }));
    await waitFor(() => expect(posts.length).toBe(1));
    expect(posts[0].url).toBe("/api/v1/assets/A1");
    const extras = posts[0].body.extras as Record<string, string>;
    expect(extras).toMatchObject({ rack: "A09", site: "DC Beograd", support: "yes" });
    expect(extras.snmp_community_set).toBeUndefined();
    expect(posts[0].body.asset_id).toBeUndefined();
  });

  it("asks before deleting an asset", async () => {
    renderPage();
    fireEvent.click(await screen.findByRole("button", { name: "esx-prod-03" }));
    fireEvent.click(await screen.findByRole("button", { name: "Edit" }));
    fireEvent.click(await screen.findByRole("button", { name: "Delete asset" }));
    expect(posts.length).toBe(0);
    fireEvent.click(screen.getByRole("button", { name: "Yes, delete" }));
    await waitFor(() => expect(posts.map((p) => p.url)).toEqual(["/api/v1/assets/A1/delete"]));
    expect(await screen.findByText("Asset deleted.")).toBeTruthy();
  });

  it("removes the selected assets and exports the filtered list", async () => {
    renderPage();
    fireEvent.click(await screen.findByRole("checkbox", { name: "Select esx-prod-03" }));
    fireEvent.click(screen.getByRole("button", { name: "Remove selected" }));
    fireEvent.click(screen.getByRole("button", { name: "Yes, remove" }));
    await waitFor(() => expect(posts.some((p) => p.url === "/api/v1/inventory/bulk-delete" && JSON.stringify(p.body) === JSON.stringify({ ids: ["A1"] }))).toBe(true));
    expect(await screen.findByText(/Removed 1 asset\./)).toBeTruthy();
    expect(screen.getByRole("link", { name: "Export CSV" }).getAttribute("href")).toContain("/api/v1/inventory.csv");
  });
});
