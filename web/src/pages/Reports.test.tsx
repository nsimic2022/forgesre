import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { Reports } from "../lib/api";
import { ReportsPage } from "./Reports";

const stats = (incidents: number, mttr: number) => ({ incidents, critical: 2, high: 3, warning: 1, mtta_min: 6, mttr_min: mttr, reviewed: 1 });
const data: Reports = {
  days: 30, from: "2026-10-20T00:00:00Z", to: "2026-11-19T00:00:00Z",
  current: stats(214, 52), previous: stats(243, 48),
  daily: [{ day: "2026-11-18", critical: 1, high: 2, other: 4 }],
  by_type: [{ group: "Network", ...stats(71, 41) }],
  noisy_assets: [{ asset: "sw-acc-03", incidents: 12 }],
  top_alerts: [{ alert: "InterfaceFlap", incidents: 9 }],
  host_down: [{ asset: "esx-prod-03", minutes: 125 }],
  scheduled: [{ id: 1, name: "Weekly NOC", kind: "performance", recipients: ["noc@dc.local"], schedule: "Every Monday 08:00", enabled: true, last_run_at: null, next_run_at: null }],
  can_edit: false,
};

describe("Reports", () => {
  let urls: string[];
  beforeEach(() => {
    urls = [];
    vi.stubGlobal("fetch", vi.fn(async (url: string) => (urls.push(url), new Response(JSON.stringify(data), { status: 200, headers: { "Content-Type": "application/json" } }))));
  });
  afterEach(() => (cleanup(), vi.unstubAllGlobals()));

  it("shows KPIs with deltas, breakdowns and scheduled reports", async () => {
    render(<ReportsPage />);
    expect(await screen.findByText("214")).toBeTruthy();
    expect(screen.getByText("−12% vs previous")).toBeTruthy();
    expect(screen.getByText("+4 min vs previous")).toBeTruthy();
    expect(screen.getByText("sw-acc-03")).toBeTruthy();
    expect(screen.getByText("Weekly NOC")).toBeTruthy();
    expect(screen.getByRole("link", { name: "Export CSV" }).getAttribute("href")).toBe("/api/v1/reports/incidents.csv?days=30");
  });

  it("changes the period", async () => {
    render(<ReportsPage />);
    fireEvent.click(await screen.findByRole("button", { name: "Last 7 days" }));
    await waitFor(() => expect(urls).toContain("/api/v1/reports-page?days=7"));
  });
});
