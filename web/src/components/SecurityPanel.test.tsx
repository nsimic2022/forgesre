import { cleanup, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { SecurityPanel } from "./SecurityPanel";

const reply = (body: unknown) => vi.stubGlobal("fetch", vi.fn(async () => new Response(JSON.stringify(body), { status: 200, headers: { "Content-Type": "application/json" } })));

describe("SecurityPanel", () => {
  afterEach(() => (cleanup(), vi.unstubAllGlobals()));

  it("shows platform and server findings with the fix, read-only", async () => {
    reply({
      platform: [{ key: "https", label: "HTTPS", status: "warn", detail: "The UI is plain HTTP.", fix: "./forgesre https on" },
        { key: "webhook_token", label: "Webhook token", status: "ok", detail: "Alerts need the token.", fix: "" }],
      host: { checked_at: "2026-11-20T08:00:00Z", as_root: false, stale: true,
        checks: [{ key: "files", label: "Secret files", status: "bad", detail: "secrets.env 644", fix: "chmod 600 secrets/secrets.env" }] },
      summary: { bad: 1, warn: 1, ok: 1, info: 0, unknown: 0 },
    });
    render(<SecurityPanel />);
    expect(await screen.findByText("The UI is plain HTTP.")).toBeTruthy();
    expect(screen.getByText("./forgesre https on")).toBeTruthy();
    expect(screen.getByText("1 problem(s)")).toBeTruthy();
    expect(screen.getByText("chmod 600 secrets/secrets.env")).toBeTruthy();
    expect(screen.getByText(/more than a week old/)).toBeTruthy();
    expect(screen.getByText(/Checked without root/)).toBeTruthy();
    expect(screen.queryByRole("button")).toBeNull();
  });

  it("says how to make the first server report", async () => {
    reply({ platform: [], host: null, summary: { bad: 0, warn: 0, ok: 0, info: 0, unknown: 0 } });
    render(<SecurityPanel />);
    expect(await screen.findByText(/No server report yet/)).toBeTruthy();
    expect(screen.getByText("Nothing to do")).toBeTruthy();
  });
});
