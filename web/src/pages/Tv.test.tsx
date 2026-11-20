import { cleanup, fireEvent, render, screen } from "@testing-library/react";
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
  daily: [{ day: "2026-11-18", critical: 1, high: 2, other: 3 }, { day: "2026-11-19", critical: 0, high: 1, other: 0 }],
  days: Array.from({ length: 7 }, (_, i) => ({ date: `2026-11-${13 + i}`, label: i === 6 ? "Today" : i === 5 ? "Yesterday" : "Mon", day: 13 + i, incidents: i, critical: i === 6 ? 1 : 0, high: 0, worst: i === 6 ? "CRITICAL" : i ? "WARNING" : "QUIET", mttr_min: null })),
  hours: { from: "2026-11-18T19:00:00+00:00", to: "2026-11-19T18:04:00+00:00", maintenance: [{ from: "2026-11-19T02:00:00+00:00", to: "2026-11-19T03:00:00+00:00", asset: "db-01", summary: "patching" }],
    points: Array.from({ length: 24 }, (_, i) => ({ at: `2026-11-19T${String(i).padStart(2, "0")}:00:00+00:00`, hour: i, open: i % 5, new: i === 22 ? 2 : 0, critical: i === 22 ? 1 : 0, high: 0, acked_fast_pct: i === 22 ? 50 : null })) },
  tiles: [
    { key: "mtta", title: "Time to acknowledge", value: 4, unit: "min", word: "On target", trend: "down", sentence: "Better: the average time to acknowledge fell.", tone: "ok", gauge: 0.13, detail: ["Target: 15 min"] },
    { key: "shift", title: "Shift", value: "Ana P.", unit: "", word: "3 h 10 min left", trend: "steady", sentence: "On call 08:00–20:00", tone: "info", gauge: 0.7, detail: ["ana@dc.local"], start: "08:00", end: "20:00" },
    { key: "noisy", title: "Noisiest assets · 24 h", value: 3, unit: "incidents", word: "esx-prod-03", trend: "steady", sentence: "x", tone: "warn", gauge: null, detail: ["esx-prod-03: 3"], bars: [{ label: "esx-prod-03", value: 3 }] },
  ],
};

describe("TV mode", () => {
  afterEach(() => (cleanup(), vi.unstubAllGlobals()));

  it("places hours like a wall clock", () => {
    expect(dialAngle(12)).toBe(0);
    expect(dialAngle(18)).toBeCloseTo(Math.PI);
    expect(dialAngle(3, 30)).toBeCloseTo((3.5 / 12) * Math.PI * 2);
  });

  it("shows day cards, the 24-hour curve and tiles, and opens a tile's detail", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => new Response(JSON.stringify(tv), { status: 200, headers: { "Content-Type": "application/json" } })));
    render(<TvPage />);
    expect(await screen.findByLabelText("Today: 6 incidents, 1 critical")).toBeTruthy();
    expect(screen.getByRole("img", { name: /Incidents open per hour, last 24 hours: 2 new/ })).toBeTruthy();
    expect(screen.getByText("50%")).toBeTruthy();
    const tile = screen.getByRole("button", { name: "Time to acknowledge: 4 min, On target" });
    fireEvent.click(tile);
    expect(screen.getByText("Target: 15 min")).toBeTruthy();
    expect(screen.getByText("On call: Ana P. · +381 60 1")).toBeTruthy();
    expect(screen.getByText("3 h 10 min left")).toBeTruthy();
  });

  it("keeps the burning list and the 12-hour clock on their own tabs", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => new Response(JSON.stringify(tv), { status: 200, headers: { "Content-Type": "application/json" } })));
    render(<TvPage />);
    fireEvent.click(await screen.findByRole("tab", { name: "Burning now" }));
    expect(await screen.findByText("Host down")).toBeTruthy();
    expect(screen.getByText("· not acked")).toBeTruthy();
    expect(screen.getByText("esx-prod-03 · INC-0412 · Tier 2")).toBeTruthy();
    fireEvent.click(screen.getByRole("tab", { name: "12-hour clock" }));
    expect(await screen.findByRole("img", { name: "18 incidents in the last 12 hours, now 18:04" })).toBeTruthy();
    expect(screen.getByRole("img", { name: "Incidents per day over 2 days: 7 in total, highest 6 in one day" })).toBeTruthy();
  });
});
