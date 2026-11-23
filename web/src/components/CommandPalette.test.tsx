import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { MemoryRouter, Route, Routes, useLocation } from "react-router-dom";
import { afterEach, describe, expect, it, vi } from "vitest";
import { CommandPalette } from "./CommandPalette";
import { CriticalNotifier } from "./CriticalNotifier";
import { saveNotifyPrefs } from "../lib/notify";

function Where() {
  const loc = useLocation();
  return <span data-testid="where">{loc.pathname + loc.search}</span>;
}

function stub(body: (url: string) => unknown) {
  const fetch = vi.fn(async (url: string) => new Response(JSON.stringify(body(url)), { status: 200, headers: { "Content-Type": "application/json" } }));
  vi.stubGlobal("fetch", fetch);
  return fetch;
}

describe("Command palette", () => {
  afterEach(() => (cleanup(), vi.unstubAllGlobals(), localStorage.clear()));

  it("Ctrl+K opens, finds an incident and Enter opens it", async () => {
    stub(() => ({ incidents: [{ number: "INC-0042_x", title: "Disk full on db01", severity: "CRITICAL", status: "OPEN" }], assets: [] }));
    render(<MemoryRouter><CommandPalette /><Routes><Route path="*" element={<Where />} /></Routes></MemoryRouter>);
    fireEvent.keyDown(window, { key: "k", ctrlKey: true });
    const box = screen.getByRole("combobox", { name: "Go to page, incident or asset" });
    fireEvent.change(box, { target: { value: "disk" } });
    expect(await screen.findByText("INC-0042 Disk full on db01")).toBeTruthy();
    fireEvent.keyDown(box, { key: "Enter" });
    expect(screen.getByTestId("where").textContent).toBe("/incidents/INC-0042_x");
    expect(screen.queryByRole("dialog")).toBeNull();
  });

  it("filters pages by name", () => {
    stub(() => ({ incidents: [], assets: [] }));
    render(<MemoryRouter><CommandPalette /></MemoryRouter>);
    fireEvent.keyDown(window, { key: "K", metaKey: true });
    fireEvent.change(screen.getByRole("combobox"), { target: { value: "rack" } });
    expect(screen.getAllByRole("option").map((o) => o.textContent)).toEqual(["RacksInventory"]);
  });
});

describe("Critical notifier", () => {
  afterEach(() => (cleanup(), vi.unstubAllGlobals(), localStorage.clear()));

  it("stays quiet until turned on, then polls from the newest incident", async () => {
    const fetch = stub(() => ({ latest: 7, items: [] }));
    render(<CriticalNotifier />);
    await new Promise((r) => setTimeout(r, 10));
    expect(fetch).not.toHaveBeenCalled();
    saveNotifyPrefs({ sound: true, desktop: false });
    await vi.waitFor(() => expect(fetch).toHaveBeenCalledWith("/api/v1/notify/critical", expect.anything()));
  });
});
