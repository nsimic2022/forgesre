import { cleanup, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { GoldenSet } from "./GoldenSet";

describe("Golden set", () => {
  afterEach(() => (cleanup(), vi.unstubAllGlobals()));

  it("shows cases with the last result and the run score", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => new Response(JSON.stringify({
      running: false, can_edit: true,
      cases: [{ id: 1, incident: "INC-0007_x", title: "Disk full", cause: "Archive logs not purged", expect: ["archive", "purged"] }],
      runs: [{ id: 3, started_at: null, finished_at: "2026-11-23T10:00:00Z", model: "qwen3-4b", skill: "best match", language: "sr", passed: 1, total: 1,
        results: [{ incident: "INC-0007", passed: true, why: "names 'archive'", cause: "Archive logs filled /u02", ms: 41000, skill: "linux-disk" }] }],
    }), { status: 200, headers: { "Content-Type": "application/json" } })));
    render(<GoldenSet />);
    expect(await screen.findByText("Archive logs not purged")).toBeTruthy();
    expect(screen.getByText("pass")).toBeTruthy();
    expect(screen.getByText("1/1")).toBeTruthy();
    expect(screen.getByRole("button", { name: "Run golden set" })).toBeTruthy();
  });
});
