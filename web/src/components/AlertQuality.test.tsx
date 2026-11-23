import { cleanup, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { AlertQuality } from "./AlertQuality";

describe("Alert quality", () => {
  afterEach(() => (cleanup(), vi.unstubAllGlobals()));

  it("lists the noisiest alerts with a suggestion", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => new Response(JSON.stringify({
      days: 30, incidents: 12, actionable_pct: 25,
      rows: [{ alertname: "NodeCPUHigh", incidents: 10, acked_pct: 10, self_cleared_pct: 80, flapping: 2, linked: 0, median_minutes: 4, assets: 3, advice: "Mostly clears by itself: add a noise filter or a longer 'for:' on the rule" }],
    }), { status: 200, headers: { "Content-Type": "application/json" } })));
    render(<AlertQuality days={30} />);
    expect(await screen.findByText("NodeCPUHigh")).toBeTruthy();
    expect(screen.getByText(/add a noise filter/)).toBeTruthy();
    expect(screen.getByText("25% of 12 incidents acknowledged by a person")).toBeTruthy();
  });
});
