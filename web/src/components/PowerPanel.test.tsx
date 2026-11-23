import { render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { PowerPanel } from "./PowerPanel";

afterEach(() => vi.unstubAllGlobals());

function stub(body: unknown) {
  vi.stubGlobal("fetch", vi.fn(async () => new Response(JSON.stringify(body), { status: 200, headers: { "Content-Type": "application/json" } })));
}

describe("PowerPanel", () => {
  it("shows a UPS on battery and a hot sensor", async () => {
    stub({
      ups: [{ asset: "ups-01", state: "critical", source: "battery", battery: "normal", charge: 92, minutes_left: 18, load: 41, input_v: 0, battery_temp: 27, alarms: 1 }],
      sensors: [{ asset: "env-01", name: "Rack A inlet", kind: "celsius", unit: "°C", value: 34.5, state: "warning" }],
      silent: [], error: "",
    });
    render(<PowerPanel />);
    expect(await screen.findByText("On battery")).toBeTruthy();
    expect(screen.getByText("18 min")).toBeTruthy();
    expect(screen.getByText("34.5 °C")).toBeTruthy();
  });

  it("renders nothing without power assets", async () => {
    stub({ ups: [], sensors: [], silent: [], error: "" });
    const { container } = render(<PowerPanel />);
    await new Promise((r) => setTimeout(r, 20));
    expect(container.textContent).toBe("");
  });
});
