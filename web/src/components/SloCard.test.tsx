import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { SloCard, type SloRow } from "./SloCard";

const row: SloRow = { id: 1, name: "ERP database", scope: "type", scope_value: "Oracle DB", target: 99.9, window_days: 30, severities: ["CRITICAL"], owner: "",
  availability: 99.861, downtime_min: 60, budget_min: 43.2, budget_left_min: -16.8, budget_used_pct: 138.9, burn_rate_24h: 0, incidents: 1, status: "breached",
  worst: [{ asset: "db-ora-02", downtime_min: 60 }] };

describe("SLO card", () => {
  afterEach(() => (cleanup(), vi.unstubAllGlobals()));
  it("shows a spent budget and saves a new SLO", async () => {
    const calls: { url: string; method: string; body: unknown }[] = [];
    vi.stubGlobal("fetch", vi.fn(async (url: string, init?: RequestInit) => {
      calls.push({ url, method: init?.method ?? "GET", body: init?.body ? JSON.parse(String(init.body)) : null });
      return new Response(JSON.stringify({ slos: [row], can_edit: true }), { status: 200, headers: { "Content-Type": "application/json" } });
    }));
    render(<SloCard />);
    expect(await screen.findByText("Budget spent")).toBeTruthy();
    expect(screen.getByText("99.861%")).toBeTruthy();
    expect(screen.getByText(/17 min over/)).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "New SLO" }));
    fireEvent.change(screen.getByLabelText("Name"), { target: { value: "Core network" } });
    fireEvent.change(screen.getByLabelText("Covers"), { target: { value: "site" } });
    fireEvent.change(screen.getByLabelText("Site"), { target: { value: "DC Beograd" } });
    fireEvent.click(screen.getByRole("button", { name: "Save SLO" }));
    await waitFor(() => expect(calls.some((c) => c.method === "POST" && c.url === "/api/v1/slos")).toBe(true));
    expect(calls.find((c) => c.method === "POST")!.body).toMatchObject({ name: "Core network", scope: "site", scope_value: "DC Beograd", target: 99.9, window_days: 30, severities: ["CRITICAL"] });
  });
});
