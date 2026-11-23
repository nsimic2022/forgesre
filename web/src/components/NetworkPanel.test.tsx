import { cleanup, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { bits, NetworkPanel } from "./NetworkPanel";

describe("NetworkPanel", () => {
  afterEach(() => (cleanup(), vi.unstubAllGlobals()));

  it("shows a BGP peer that is down and the busiest interfaces", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => new Response(JSON.stringify({
      error: "",
      bgp: [{ asset: "rtr-01", peer: "10.0.0.2", remote_as: 65001, state: "active", enabled: true, established_s: 0, ok: false }],
      ospf: [{ asset: "rtr-01", neighbour: "10.0.1.2", state: "full", ok: true }],
      top_in: [{ asset: "sw-core", interface: "Te1/0/1", alias: "uplink", bps: 2.5e9 }],
      top_out: [{ asset: "sw-core", interface: "Te1/0/2", alias: "", bps: 4.2e6 }],
    }), { status: 200, headers: { "Content-Type": "application/json" } })));
    render(<NetworkPanel />);
    expect(await screen.findByText("BGP rtr-01 → 10.0.0.2 (AS 65001) active")).toBeTruthy();
    expect(screen.getByText("2.5 Gb/s")).toBeTruthy();
    expect(screen.getByText("4.2 Mb/s")).toBeTruthy();
  });

  it("formats bits", () => {
    expect(bits(999)).toBe("999 b/s");
    expect(bits(1500)).toBe("1.5 kb/s");
  });
});
