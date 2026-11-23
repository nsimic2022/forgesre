import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { IncidentKnownErrors } from "./KnownErrors";
import { SavedViews } from "./SavedViews";

function stub(body: (url: string, init?: RequestInit) => unknown, posts: unknown[] = []) {
  vi.stubGlobal("fetch", vi.fn(async (url: string, init?: RequestInit) => {
    if (init?.method === "POST") posts.push(JSON.parse(String(init.body)));
    return new Response(JSON.stringify(body(url, init)), { status: 200, headers: { "Content-Type": "application/json" } });
  }));
}

describe("Known errors and saved views", () => {
  afterEach(() => (cleanup(), vi.unstubAllGlobals()));

  it("shows the workaround of a matching known error", () => {
    render(<IncidentKnownErrors items={[{ id: 1, title: "Battery learn cycle", alertname: "DiskLatency", pattern: "", symptoms: "", workaround: "Wait 2 h", fix: "", link: "", status: "open", created_by: "", updated_at: null }]}
      canEdit={false} title="x" alertname="DiskLatency" cause="" onChanged={() => undefined} />);
    expect(screen.getByText("Battery learn cycle")).toBeTruthy();
    expect(screen.getByText("Wait 2 h")).toBeTruthy();
  });

  it("Save as known error is prefilled from the incident", async () => {
    const posts: unknown[] = [];
    stub(() => ({ id: 2 }), posts);
    render(<IncidentKnownErrors items={[]} canEdit title="Disk latency on db01" alertname="DiskLatency" cause="RAID battery" onChanged={() => undefined} />);
    fireEvent.click(screen.getByRole("button", { name: "Save as known error" }));
    expect((screen.getByPlaceholderText("NodeDiskWriteLatencyHigh") as HTMLInputElement).value).toBe("DiskLatency");
    fireEvent.click(screen.getByRole("button", { name: "Save" }));
    await vi.waitFor(() => expect(posts[0]).toMatchObject({ title: "Disk latency on db01", alertname: "DiskLatency", workaround: "RAID battery" }));
  });

  it("applies a saved view and saves the current filters", async () => {
    const posts: unknown[] = [];
    stub((url) => (url.startsWith("/api/v1/views") ? { can_save: true, can_share: true, views: [{ id: 1, name: "Net critical", filters: { severity: "CRITICAL", team: "network" }, team: "network", mine: false, can_delete: false }] } : {}), posts);
    const applied: unknown[] = [];
    render(<SavedViews page="incidents" current={{ tab: "active", severity: "" }} teams={["network"]} onApply={(f) => applied.push(f)} />);
    fireEvent.click(await screen.findByRole("button", { name: /Net critical/ }));
    expect(applied[0]).toEqual({ severity: "CRITICAL", team: "network" });
    fireEvent.click(screen.getByRole("button", { name: "Save view" }));
    fireEvent.change(screen.getByLabelText("View name"), { target: { value: "Mine" } });
    fireEvent.change(screen.getByLabelText("Share with"), { target: { value: "network" } });
    fireEvent.click(screen.getByRole("button", { name: "Save" }));
    await vi.waitFor(() => expect(posts[0]).toEqual({ page: "incidents", name: "Mine", team: "network", filters: { tab: "active", severity: "" } }));
  });
});
