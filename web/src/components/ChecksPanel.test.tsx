import { cleanup, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { ChecksPanel } from "./ChecksPanel";

describe("Checks panel", () => {
  afterEach(() => (cleanup(), vi.unstubAllGlobals()));
  it("lists failing checks first", async () => {
    const body = { results: [
      { kind: "tls", target: "erp.dc:443", status: "ok", value: 80, detail: "certificate expires in 80.0 days", checked_at: null },
      { kind: "backup", target: "forgesre", status: "critical", value: 50, detail: "newest ForgeSRE backup is 50.0 h old (limit 36 h)", checked_at: null },
    ], heartbeats: [], tls_targets: ["erp.dc:443"], can_edit: true };
    vi.stubGlobal("fetch", vi.fn(async () => new Response(JSON.stringify(body), { status: 200, headers: { "Content-Type": "application/json" } })));
    render(<ChecksPanel />);
    await screen.findByText("forgesre");
    const rows = screen.getAllByRole("row");
    expect(rows[1].textContent).toContain("critical");
    expect(rows[2].textContent).toContain("erp.dc:443");
    expect(screen.getByRole("button", { name: "Run now" })).toBeTruthy();
  });
});
