import { cleanup, render, screen } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { afterEach, describe, expect, it, vi } from "vitest";
import { LifecycleCard, RacksPage } from "./Racks";

function stub(body: unknown) {
  vi.stubGlobal("fetch", vi.fn(async () => new Response(JSON.stringify(body), { status: 200, headers: { "Content-Type": "application/json" } })));
}

describe("Racks and lifecycle", () => {
  afterEach(() => (cleanup(), vi.unstubAllGlobals()));

  it("draws a rack with devices, unplaced devices and overlaps", async () => {
    stub({ units: 42, racks: [{ site: "DC1", row: "A", rack: "A07", units: 42, used_u: 3, too_high: [],
      devices: [{ asset_id: "esx-01", hostname: "esx-01", type: "Hypervisor", u: 10, height: 2, status: "healthy", incident: "CRITICAL" },
        { asset_id: "sw-01", hostname: "sw-01", type: "Switch", u: 42, height: 1, status: "healthy", incident: "" }],
      unplaced: [{ asset_id: "pdu-1", hostname: "pdu-1", type: "PDU", u: null, height: 1, status: "healthy", incident: "" }],
      overlaps: ["U11: esx-01 and nas-01"] }] });
    render(<MemoryRouter><RacksPage /></MemoryRouter>);
    expect(await screen.findByText("DC1 / A / A07")).toBeTruthy();
    expect(screen.getAllByRole("listitem")).toHaveLength(2);
    expect(screen.getByText("No position: pdu-1")).toBeTruthy();
    expect(screen.getByText(/Overlap: U11/)).toBeTruthy();
  });

  it("lists expiring support and licences", async () => {
    stub({ tracked: 4, warn_days: 30, rows: [{ asset: "fw-01", hostname: "fw-01", kind: "license", label: "Licence", date: "2026-11-20", days_left: -3, status: "expired" },
      { asset: "esx-01", hostname: "esx-01", kind: "support", label: "Support / warranty", date: "2026-12-01", days_left: 8, status: "expiring" }] });
    render(<LifecycleCard />);
    expect(await screen.findByText("3 d ago")).toBeTruthy();
    expect(screen.getByText("in 8 d")).toBeTruthy();
  });
});
