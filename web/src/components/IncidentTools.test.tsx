import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { afterEach, describe, expect, it, vi } from "vitest";
import type { NocIncident } from "../lib/api";
import { IncidentLinks } from "./IncidentLinks";
import { IncidentTable } from "./IncidentTable";

const row = (n: string): NocIncident => ({ number: n, title: `Disk ${n}`, severity: "HIGH", status: "OPEN", started_at: null, resolved_at: null, ack_at: null, ack_by: "", asset: null, source: "prometheus", level: 1, messages: 0, forgerca: "", forgeai: "none", detail: "" } as unknown as NocIncident);

function stub(posts: { url: string; body: unknown }[], reply: unknown = { done: [], missing: [] }) {
  vi.stubGlobal("fetch", vi.fn(async (url: string, init?: RequestInit) => {
    posts.push({ url, body: JSON.parse(String(init?.body ?? "{}")) });
    return new Response(JSON.stringify(reply), { status: 200, headers: { "Content-Type": "application/json" } });
  }));
}

describe("incident tools", () => {
  afterEach(() => (cleanup(), vi.unstubAllGlobals()));

  it("acknowledges the selected incidents at once", async () => {
    const posts: { url: string; body: unknown }[] = [];
    stub(posts);
    const changed = vi.fn();
    render(<MemoryRouter><IncidentTable rows={[row("INC-1"), row("INC-2"), row("INC-3")]} loaded canWork onChanged={changed} /></MemoryRouter>);
    fireEvent.click(screen.getByLabelText("Select INC-1"));
    fireEvent.click(screen.getByLabelText("Select INC-3"));
    expect(screen.getByText("2 selected")).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "Acknowledge selected" }));
    await waitFor(() => expect(changed).toHaveBeenCalled());
    expect(posts[0]).toEqual({ url: "/api/v1/incidents/bulk", body: { numbers: ["INC-1", "INC-3"], action: "ack" } });
  });

  it("links a cause and quiets the host with a reason", async () => {
    const posts: { url: string; body: unknown }[] = [];
    stub(posts, { asset: "db-01", until: "2026-11-23T12:00:00+01:00", caused_by: "INC-9" });
    render(<MemoryRouter><IncidentLinks number="INC-5" links={{ caused_by: null, consequences: [] }} canWork canQuiet hasAsset onChanged={() => undefined} /></MemoryRouter>);
    fireEvent.change(screen.getByLabelText("Caused by incident"), { target: { value: "INC-9" } });
    fireEvent.click(screen.getByRole("button", { name: "Link" }));
    await waitFor(() => expect(posts[0]?.url).toBe("/api/v1/incidents/INC-5/caused-by"));
    expect(screen.getByRole("button", { name: "Start maintenance" }).hasAttribute("disabled")).toBe(true);
    fireEvent.change(screen.getByLabelText("Why"), { target: { value: "PSU swap" } });
    fireEvent.click(screen.getByRole("button", { name: "Start maintenance" }));
    await waitFor(() => expect(posts[1]).toEqual({ url: "/api/v1/incidents/INC-5/quiet-host", body: { hours: 2, reason: "PSU swap" } }));
    expect(await screen.findByText(/db-01 is in maintenance until/)).toBeTruthy();
  });
});
