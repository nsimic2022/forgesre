import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { SourcesPanel } from "./SourcesPanel";

const data = {
  can_edit: true, new_devices: "review",
  sources: [{ source: "glpi", enabled: true, linked: 12, conflicts: 1, last: { at: "2026-11-20T06:00:00Z", status: "ok", summary: "GLPI sync: 0 new, 12 linked" } }],
  conflicts: [{ id: 4, source: "glpi", source_id: "Computer:9", kind: "field", field: "Site", ours: "DC2 room B", theirs: "DC3", detail: "Changed in ForgeSRE after glpi wrote it.", asset_id: "srv-01", hostname: "srv-01", seen_at: null }],
};

describe("SourcesPanel", () => {
  afterEach(() => (cleanup(), vi.unstubAllGlobals()));

  it("shows each feeder and takes the source value on request", async () => {
    const posts: { url: string; body: unknown }[] = [];
    vi.stubGlobal("fetch", vi.fn(async (url: string, init?: RequestInit) => {
      if (init?.method === "POST") posts.push({ url, body: JSON.parse(String(init.body)) });
      return new Response(JSON.stringify(init?.method === "POST" ? { id: 4, status: "theirs" } : data), { status: 200, headers: { "Content-Type": "application/json" } });
    }));
    const changed = vi.fn();
    render(<SourcesPanel onClose={() => undefined} onChanged={changed} />);
    expect(await screen.findByText("GLPI sync: 0 new, 12 linked")).toBeTruthy();
    expect(screen.getByText("listed in Discovery first")).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "Take GLPI value" }));
    await waitFor(() => expect(changed).toHaveBeenCalled());
    expect(posts[0]).toEqual({ url: "/api/v1/inventory/conflicts/4", body: { action: "theirs" } });
  });
});
