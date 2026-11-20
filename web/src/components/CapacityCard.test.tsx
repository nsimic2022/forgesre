import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { CapacityCard } from "./CapacityCard";

const ready = {
  state: "ready", measured_at: "2026-11-20T08:00:00Z", soonest_days: 9, warn: true,
  rows: [
    { label: "Disk", state: "forecast", note: "~9 days to full", spark: "0,20 80,2", forecast_days: 9, points: 720 },
    { label: "Interface receive", state: "spark", note: "30-day history", spark: "0,10 80,12", forecast_days: null, points: 720 },
  ],
};

describe("CapacityCard", () => {
  afterEach(() => (cleanup(), vi.unstubAllGlobals()));

  it("waits for the background read, then shows days to full", async () => {
    const urls: string[] = [];
    let calls = 0;
    vi.useFakeTimers({ shouldAdvanceTime: true });
    vi.stubGlobal("fetch", vi.fn(async (url: string) => {
      urls.push(url);
      calls += 1;
      const body = calls === 1 ? { state: "pending", job: 4 } : ready;
      return new Response(JSON.stringify(body), { status: 200, headers: { "Content-Type": "application/json" } });
    }));
    render(<CapacityCard assetId="web 01" />);
    expect(await screen.findByText(/Reading 30 days/)).toBeTruthy();
    await vi.advanceTimersByTimeAsync(1600);
    expect(await screen.findByText("~9 days to full")).toBeTruthy();
    expect(screen.getByText("30-day history")).toBeTruthy();
    expect(urls[0]).toBe("/api/v1/assets/web%2001/capacity");
    fireEvent.click(screen.getByRole("button", { name: "Read again" }));
    await waitFor(() => expect(urls.some((u) => u.endsWith("?refresh=true"))).toBe(true));
    vi.useRealTimers();
  });

  it("explains a failed read", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => new Response(JSON.stringify({ state: "error", error: "prometheus down" }), { status: 200, headers: { "Content-Type": "application/json" } })));
    render(<CapacityCard assetId="web-01" />);
    expect(await screen.findByText(/prometheus down/)).toBeTruthy();
  });
});
