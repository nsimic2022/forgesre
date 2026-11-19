import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { OncallWeek } from "../lib/api";
import { OnCallPage } from "./OnCall";

const slot = { id: 7, weekday: 0, day: "Monday", start: "08:00", end: "16:00", start_minute: 480, end_minute: 960, name: "Ana", surname: "P.", person: "Ana P.", email: "ana@dc.local", phone: "+381 60 1", phone2: "", enabled: true, window: "Monday 08:00–16:00" };
const week: OncallWeek = {
  weekdays: ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"],
  slots: [slot], now: slot, timezone: "Europe/Belgrade",
  people: [{ name: "Marko", surname: "J.", label: "Marko J.", email: "marko@dc.local", phone: "+381 60 2", phone2: "" }],
  can_edit: true,
};

describe("On-call week", () => {
  let calls: { url: string; method: string; body: Record<string, unknown> | null }[];
  beforeEach(() => {
    calls = [];
    vi.stubGlobal("fetch", vi.fn(async (url: string, init?: RequestInit) => {
      calls.push({ url, method: init?.method ?? "GET", body: init?.body ? JSON.parse(String(init.body)) : null });
      return new Response(JSON.stringify(url === "/api/v1/oncall-week" ? week : { id: 9 }), { status: 200, headers: { "Content-Type": "application/json" } });
    }));
  });
  afterEach(() => (cleanup(), vi.unstubAllGlobals()));
  const renderPage = () => render(<MemoryRouter><OnCallPage /></MemoryRouter>);

  it("shows who is on call now and the slot in the Monday column", async () => {
    renderPage();
    expect(await screen.findByText("Monday 08:00–16:00 · +381 60 1 · ana@dc.local")).toBeTruthy();
    expect(screen.getByRole("list", { name: "Monday on-call" }).textContent).toContain("Ana P.");
    expect(screen.getByRole("list", { name: "Tuesday on-call" }).textContent).toBe("");
  });

  it("adds a window on several days, filling contacts from a known person", async () => {
    renderPage();
    fireEvent.click(await screen.findByRole("button", { name: "New window" }));
    fireEvent.change(screen.getByLabelText("Day"), { target: { value: "5" } });
    fireEvent.click(screen.getByRole("checkbox", { name: "Sun" }));
    fireEvent.change(screen.getByLabelText("Name"), { target: { value: "Marko J." } });
    expect((screen.getByLabelText("Email") as HTMLInputElement).value).toBe("marko@dc.local");
    fireEvent.click(screen.getByRole("button", { name: "Add window" }));
    await waitFor(() => expect(calls.filter((c) => c.method === "POST" && c.url === "/api/v1/oncall-slots").length).toBe(2));
    const days = calls.filter((c) => c.url === "/api/v1/oncall-slots").map((c) => c.body?.weekday);
    expect(days).toEqual([5, 6]);
    expect(calls.find((c) => c.url === "/api/v1/oncall-slots")!.body).toMatchObject({ name: "Marko", email: "marko@dc.local", start: "08:00" });
  });

  it("turns a window off and removes one only after asking", async () => {
    renderPage();
    fireEvent.click(await screen.findByRole("listitem", { name: /Monday 08:00–16:00 Ana P./ }));
    fireEvent.click(screen.getByRole("button", { name: "Turn off" }));
    await waitFor(() => expect(calls.some((c) => c.url === "/api/v1/oncall-slots/7/toggle")).toBe(true));
    fireEvent.click(await screen.findByRole("listitem", { name: /Monday 08:00–16:00 Ana P./ }));
    fireEvent.click(screen.getByRole("button", { name: "Remove" }));
    expect(calls.some((c) => c.method === "DELETE")).toBe(false);
    fireEvent.click(screen.getByRole("button", { name: "Yes, remove" }));
    await waitFor(() => expect(calls.some((c) => c.method === "DELETE" && c.url === "/api/v1/oncall-slots/7")).toBe(true));
  });
});
