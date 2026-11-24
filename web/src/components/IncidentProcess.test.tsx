import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { HandoverPart } from "./Handover";
import { FollowUpActions, MajorIncidentCard, SlaChips } from "./IncidentProcess";

function stub() {
  const posts: { url: string; body: unknown; method: string }[] = [];
  vi.stubGlobal("fetch", vi.fn(async (url: string, init?: RequestInit) => {
    if (init?.method && init.method !== "GET") posts.push({ url, body: init.body ? JSON.parse(String(init.body)) : null, method: init.method });
    return new Response("{}", { status: 200, headers: { "Content-Type": "application/json" } });
  }));
  return posts;
}

describe("Incident process", () => {
  afterEach(() => (cleanup(), vi.unstubAllGlobals()));

  it("declares a major incident and posts a status update", async () => {
    const posts = stub();
    const changed = vi.fn();
    const off = { on: false, commander: "", bridge_url: "", update_minutes: 30, last_update_at: null, next_due: null, overdue: false, message: "" };
    render(<MajorIncidentCard number="INC-7" major={off} canWork onChanged={changed} />);
    fireEvent.click(screen.getByRole("button", { name: "Declare major incident" }));
    fireEvent.change(screen.getByLabelText("Bridge call link"), { target: { value: "https://meet.example/x" } });
    fireEvent.click(screen.getByRole("button", { name: "Declare" }));
    await waitFor(() => expect(posts[0]).toMatchObject({ url: "/api/v1/incidents/INC-7/major", body: { on: true, bridge_url: "https://meet.example/x", update_minutes: 30 } }));
    cleanup();
    const on = { ...off, on: true, commander: "Ana", next_due: new Date(Date.now() - 120000).toISOString(), overdue: true, message: "[MAJOR INCIDENT] INC-7" };
    render(<MajorIncidentCard number="INC-7" major={on} canWork onChanged={changed} />);
    expect(screen.getByText(/update overdue/)).toBeTruthy();
    fireEvent.change(screen.getByLabelText("Status update"), { target: { value: "Rebooting the array" } });
    fireEvent.click(screen.getByRole("button", { name: "Post status update" }));
    await waitFor(() => expect(posts.at(-1)).toMatchObject({ url: "/api/v1/incidents/INC-7/updates", body: { text: "Rebooting the array" } }));
  });

  it("shows SLA chips", () => {
    render(<SlaChips sla={{ response: { state: "breached" }, resolve: { state: "running", minutes_left: 90, target_min: 240 } }} />);
    expect(screen.getByText("Response breached")).toBeTruthy();
    expect(screen.getByText("Resolve due in 1 h 30 min")).toBeTruthy();
  });

  it("adds and ticks follow-up actions", async () => {
    const posts = stub();
    render(<FollowUpActions number="INC-7" canWork onChanged={() => undefined}
      actions={[{ id: 3, text: "Second uplink", owner: "net", due: "2020-01-01", done: false, done_at: null, created_by: "a", overdue: true }]} />);
    expect(screen.getByText(/overdue 2020-01-01/)).toBeTruthy();
    fireEvent.click(screen.getByLabelText("Done: Second uplink"));
    await waitFor(() => expect(posts[0]).toMatchObject({ url: "/api/v1/actions/3", method: "PATCH", body: { done: true } }));
    fireEvent.change(screen.getByLabelText("New action"), { target: { value: "Alert on UPS battery age" } });
    fireEvent.click(screen.getByRole("button", { name: "Add" }));
    await waitFor(() => expect(posts.at(-1)).toMatchObject({ url: "/api/v1/incidents/INC-7/actions", body: { text: "Alert on UPS battery age" } }));
  });

  it("offers I take over only to someone else", async () => {
    const posts = stub();
    const h = { id: 9, created_by: "ana@dc.local", created_at: null, notes: "Watch the backup", open_incidents: [], taken_by: "", taken_at: null };
    const { rerender } = render(<HandoverPart handover={h} canWork me="ana@dc.local" onChanged={() => undefined} />);
    expect(screen.queryByRole("button", { name: "I take over" })).toBeNull();
    rerender(<HandoverPart handover={h} canWork me="marko@dc.local" onChanged={() => undefined} />);
    fireEvent.click(screen.getByRole("button", { name: "I take over" }));
    await waitFor(() => expect(posts[0].url).toBe("/api/v1/handover/9/take"));
  });
});
