import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { IntegrationMapPage } from "./IntegrationMap";

const now = new Date().toISOString();
const step = (id: string, status: string, detail = "") => ({ id, label: id.toUpperCase(), sub: "", status, detail, fix: "" });
const flows = [
  { name: "prometheus", label: "Prometheus", about: "", configured: true, status: "done", result: { checked_at: now, lanes: [{ id: "in", title: "Prometheus in", steps: [step("sd", "ok"), step("targets", "ok")] }] } },
  { name: "zabbix", label: "Zabbix", about: "", configured: true, status: "done", result: { checked_at: now, lanes: [{ id: "in", title: "Zabbix in", steps: [step("media", "ok"), step("sent", "bad", "Couldn't connect to server")] }] } },
  { name: "mcp", label: "MCP server", about: "AI clients.", configured: false, status: "none", result: null },
];

describe("Integration map", () => {
  let posts: string[];
  beforeEach(() => {
    posts = [];
    vi.stubGlobal("fetch", vi.fn(async (url: string, init?: RequestInit) => {
      if (init?.method === "POST") posts.push(url);
      return new Response(JSON.stringify(url === "/api/v1/flows" ? flows : { job: 1 }), { status: 200, headers: { "Content-Type": "application/json" } });
    }));
  });
  afterEach(() => (cleanup(), vi.unstubAllGlobals()));

  it("puts broken flows first with the broken block, and checks only set-up flows", async () => {
    render(<MemoryRouter><IntegrationMapPage /></MemoryRouter>);
    const cards = await screen.findAllByRole("link", { name: /: (Broken|Works|Not set up)$/ });
    expect(cards.map((c) => c.getAttribute("aria-label"))).toEqual(["Zabbix: Broken", "Prometheus: Works", "MCP server: Not set up"]);
    expect(cards[0].getAttribute("href")).toBe("/settings/integrations/zabbix");
    expect(cards[0].textContent).toContain("Couldn't connect to server");
    expect(screen.getByText(/1 flow\(s\) broken/)).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "Check all set-up flows" }));
    await waitFor(() => expect(posts).toEqual(["/api/v1/integrations/prometheus/flow", "/api/v1/integrations/zabbix/flow"]));
  });
});
