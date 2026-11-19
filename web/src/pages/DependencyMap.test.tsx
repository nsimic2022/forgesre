import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { Topology } from "../lib/api";
import { DependencyMapPage } from "./DependencyMap";

const leaf = (id: string, alarm = false) => ({ asset_id: id, hostname: id, type: "Linux Server", ip: "", alarm, network: false, behind: 0, children: [] });
const topo: Topology = {
  roots: [{ asset_id: "sw-acc-07", hostname: "sw-acc-07", type: "Switch", ip: "10.0.1.7", alarm: true, network: true, behind: 2, children: [leaf("esx-01"), leaf("esx-02")] }],
  unlinked: [leaf("db-ora-02")],
  assets: 4, with_parent: 2, links: 3, last_sync: "2026-11-19T10:00:00Z",
  suggestions: [{ asset_id: "db-ora-02", hostname: "db-ora-02", parent_asset_id: "sw-acc-07", parent_hostname: "sw-acc-07", current_parent: "", protocol: "lldp", port: "Gi1/0/14" }],
  can_edit: true,
};

describe("Dependency map", () => {
  let calls: { url: string; method: string; body: unknown }[];
  beforeEach(() => {
    calls = [];
    vi.stubGlobal("fetch", vi.fn(async (url: string, init?: RequestInit) => {
      calls.push({ url, method: init?.method ?? "GET", body: init?.body ? JSON.parse(String(init.body)) : null });
      return new Response(JSON.stringify(url === "/api/v1/topology" ? topo : { ok: true }), { status: 200, headers: { "Content-Type": "application/json" } });
    }));
  });
  afterEach(() => (cleanup(), vi.unstubAllGlobals()));
  const renderPage = () => render(<MemoryRouter><DependencyMapPage /></MemoryRouter>);

  it("shows the tree with how much hides behind a node", async () => {
    renderPage();
    expect(await screen.findByText("2 behind it")).toBeTruthy();
    expect(screen.getByText("esx-01")).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "Collapse sw-acc-07" }));
    expect(screen.queryByText("esx-01")).toBeNull();
  });

  it("accepts a suggested parent and starts a sync", async () => {
    renderPage();
    fireEvent.click(await screen.findByRole("button", { name: "Accept sw-acc-07 as parent of db-ora-02" }));
    await waitFor(() => expect(calls.some((c) => c.url === "/api/v1/topology/parent")).toBe(true));
    expect(calls.find((c) => c.url === "/api/v1/topology/parent")!.body).toEqual({ asset_id: "db-ora-02", parent_asset_id: "sw-acc-07" });
    fireEvent.click(screen.getByRole("button", { name: "Sync from LibreNMS" }));
    await waitFor(() => expect(calls.some((c) => c.method === "POST" && c.url === "/api/v1/topology/sync")).toBe(true));
  });

  it("sets a parent by hostname and refuses an unknown one", async () => {
    renderPage();
    fireEvent.click(await screen.findByRole("button", { name: "Change parent of esx-01" }));
    fireEvent.change(screen.getByLabelText("Parent of esx-01"), { target: { value: "nope" } });
    fireEvent.click(screen.getByRole("button", { name: "Set" }));
    expect(await screen.findByText("No asset with that name")).toBeTruthy();
    fireEvent.change(screen.getByLabelText("Parent of esx-01"), { target: { value: "db-ora-02" } });
    fireEvent.click(screen.getByRole("button", { name: "Set" }));
    await waitFor(() => expect(calls.some((c) => c.url === "/api/v1/topology/parent")).toBe(true));
  });
});
