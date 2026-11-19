import { cleanup, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { dialAngle } from "../components/IncidentClock";
import type { TvData } from "../lib/api";
import { TvPage } from "./Tv";

const hours = Array.from({ length: 12 }, (_, i) => ({ start: `2026-11-19T${String(7 + i).padStart(2, "0")}:00:00+01:00`, hour: 7 + i, critical: i === 11 ? 1 : 0, high: i % 3 === 0 ? 2 : 0, warning: 1 }));
const tv: TvData = {
  generated_at: "2026-11-19T18:04:00+01:00",
  counts: { open: 3, critical: 1, high: 1, warning: 1, not_acked: 2, hosts_total: 412, hosts_down: 1, hosts_down_names: ["esx-prod-03"], mtta_today_min: 4, mttr_7d_min: 38 },
  incidents: {
    active: [{ number: "INC-0412", severity: "CRITICAL", status: "OPEN", title: "Host down", detail: "", source: "prometheus", started_at: new Date().toISOString(), ack_at: null, ack_by: "", resolved_at: null, asset: { hostname: "esx-prod-03", type: "Hypervisor", ip: "10.0.0.3" }, level: "L2", messages: 3, forgerca: "", forgeai: "ready-local" }],
    acknowledged: [], resolved_today: [],
  },
  health: [], oncall: { name: "Ana P.", email: "ana@dc.local", phone: "+381 60 1" }, ai: { forgerca: true, forgeai: "local" },
  clock: { now: "2026-11-19T18:04:00+01:00", hour: 18, minute: 4, timezone: "Europe/Belgrade", hours, total: 18 },
};

describe("TV mode", () => {
  afterEach(() => (cleanup(), vi.unstubAllGlobals()));

  it("places hours like a wall clock", () => {
    expect(dialAngle(12)).toBe(0);
    expect(dialAngle(18)).toBeCloseTo(Math.PI);
    expect(dialAngle(3, 30)).toBeCloseTo((3.5 / 12) * Math.PI * 2);
  });

  it("shows the numbers, the clock, who is on call and what burns", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => new Response(JSON.stringify(tv), { status: 200, headers: { "Content-Type": "application/json" } })));
    render(<TvPage />);
    expect(await screen.findByRole("img", { name: "18 incidents in the last 12 hours, now 18:04" })).toBeTruthy();
    expect(screen.getByText("Ana P.")).toBeTruthy();
    expect(screen.getByText("Host down")).toBeTruthy();
    expect(screen.getByText("· not acked")).toBeTruthy();
    expect(screen.getByText("now 18:04 · 18 in 12 h")).toBeTruthy();
    expect(screen.getAllByRole("row").length).toBe(13);
  });
});
