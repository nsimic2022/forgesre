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

  it("schedules a ForgeAI morning digest", async () => {
    const calls: { url: string; method: string; body: Record<string, unknown> | null }[] = [];
    vi.stubGlobal("fetch", vi.fn(async (url: string, init?: RequestInit) => {
      calls.push({ url, method: init?.method ?? "GET", body: init?.body ? JSON.parse(String(init.body)) : null });
      return new Response(JSON.stringify({ ...data, can_edit: true }), { status: 200, headers: { "Content-Type": "application/json" } });
    }));
    render(<ReportsPage />);
    fireEvent.click(await screen.findByRole("button", { name: "New schedule" }));
    fireEvent.change(screen.getByRole("combobox", { name: /What/ }), { target: { value: "forgeai" } });
    fireEvent.change(screen.getByLabelText("Recipients (comma separated)"), { target: { value: "noc@dc.local" } });
    fireEvent.click(screen.getByRole("button", { name: "Save schedule" }));
    await waitFor(() => expect(calls.some((c) => c.method === "POST" && c.url === "/api/v1/scheduled-reports")).toBe(true));
    expect(calls.find((c) => c.method === "POST")!.body).toMatchObject({ kind: "forgeai", name: "Morning ForgeAI digest", schedule: "cron", cron: "0 7 * * *" });
    expect(screen.getAllByText("NOC summary").length).toBeGreaterThan(0);
  });

  it("one period picker drives the numbers, the chart and the CSV", async () => {
    const trend = (range: string) => ({ range, unit: range === "today" ? "hour" : range === "90d" ? "week" : "day", total: 3,
      buckets: [{ day: range === "90d" ? "28.10–03.11" : "2026-11-18", critical: 1, high: 1, other: 1 }] });
    vi.stubGlobal("fetch", vi.fn(async (url: string) => {
      urls.push(url);
      const m = /range=(\w+)/.exec(url);
      return new Response(JSON.stringify(m ? trend(m[1]) : data), { status: 200, headers: { "Content-Type": "application/json" } });
    }));
    render(<ReportsPage />);
    expect(await screen.findByText("Incidents per day, last 30 days")).toBeTruthy();
    expect(screen.queryByRole("group", { name: "Chart range" })).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: "Last 90 days" }));
    expect(await screen.findByText("Incidents per week, last 90 days")).toBeTruthy();
    expect(await screen.findByRole("img", { name: /Incidents per week over 1 weeks/ })).toBeTruthy();
    await waitFor(() => expect(urls).toContain("/api/v1/reports-page?days=90"));
    fireEvent.click(screen.getByRole("button", { name: "Today" }));
    await waitFor(() => expect(urls).toContain("/api/v1/reports/incident-trend?range=today"));
    expect(urls).toContain("/api/v1/reports-page?days=0");
    expect(screen.getByRole("link", { name: "Export CSV" }).getAttribute("href")).toBe("/api/v1/reports/incidents.csv?days=0");
    expect(screen.getByRole("navigation", { name: "Report sections" })).toBeTruthy();
    expect(screen.getByText("SLA · how fast we react")).toBeTruthy();
  });

  it("changes the period", async () => {
    render(<ReportsPage />);
    fireEvent.click(await screen.findByRole("button", { name: "Last 7 days" }));
    await waitFor(() => expect(urls).toContain("/api/v1/reports-page?days=7"));
  });

  it("creates a cron schedule from an example", async () => {
    const calls: { url: string; method: string; body: Record<string, unknown> | null }[] = [];
    vi.stubGlobal("fetch", vi.fn(async (url: string, init?: RequestInit) => {
      calls.push({ url, method: init?.method ?? "GET", body: init?.body ? JSON.parse(String(init.body)) : null });
      return new Response(JSON.stringify({ ...data, can_edit: true }), { status: 200, headers: { "Content-Type": "application/json" } });
    }));
    render(<ReportsPage />);
    fireEvent.click(await screen.findByRole("button", { name: "New schedule" }));
    fireEvent.change(screen.getByLabelText("Recipients (comma separated)"), { target: { value: "noc@dc.local, boss@dc.local" } });
    fireEvent.click(screen.getByRole("button", { name: "1st of the month 08:00" }));
    fireEvent.click(screen.getByRole("button", { name: "Save schedule" }));
    await waitFor(() => expect(calls.some((c) => c.method === "POST" && c.url === "/api/v1/scheduled-reports")).toBe(true));
    expect(calls.find((c) => c.method === "POST")!.body).toMatchObject({ schedule: "cron", cron: "0 8 1 * *", recipients: ["noc@dc.local", "boss@dc.local"] });
  });

  it("previews a scheduled report and sends it now in the background", async () => {
    const calls: { url: string; method: string }[] = [];
    vi.stubGlobal("fetch", vi.fn(async (url: string, init?: RequestInit) => {
      calls.push({ url, method: init?.method ?? "GET" });
      const body = url.endsWith("/preview")
        ? { subject: "Report Weekly NOC", text: "", html: "<html>at a glance</html>", note: "", recipients: ["noc@dc.local"], schedule: "Every Monday 08:00" }
        : url.endsWith("/send") ? { queued: true } : { ...data, can_edit: true };
      return new Response(JSON.stringify(body), { status: 200, headers: { "Content-Type": "application/json" } });
    }));
    render(<ReportsPage />);
    fireEvent.click(await screen.findByRole("button", { name: "Preview Weekly NOC" }));
    expect(await screen.findByTitle("Scheduled report preview")).toBeTruthy();
    expect(screen.getByText("Every Monday 08:00 · to noc@dc.local")).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "Send now" }));
    expect(await screen.findByRole("button", { name: "Queued, sending…" })).toBeTruthy();
    expect(calls.some((c) => c.method === "POST" && c.url === "/api/v1/scheduled-reports/1/send")).toBe(true);
  });
});
