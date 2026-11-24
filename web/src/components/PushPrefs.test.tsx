import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { afterEach, describe, expect, it, vi } from "vitest";
import { PushPrefs } from "./AppearancePanel";
import { CommandPalette } from "./CommandPalette";

function stub(body: unknown) {
  vi.stubGlobal("fetch", vi.fn(async () => new Response(JSON.stringify(body), { status: 200, headers: { "Content-Type": "application/json" } })));
}

describe("Push and palette", () => {
  afterEach(() => (cleanup(), vi.unstubAllGlobals()));

  it("says push is off; an Admin can turn it on from the card", async () => {
    const calls: { url: string; method: string; body: string }[] = [];
    let on = false;
    vi.stubGlobal("fetch", vi.fn(async (url: string, init?: RequestInit) => {
      calls.push({ url, method: init?.method ?? "GET", body: String(init?.body ?? "") });
      if (init?.method === "PUT") on = true;
      return new Response(JSON.stringify({ enabled: on, public_key: on ? "BAAA" : "", devices: 0 }), { status: 200, headers: { "Content-Type": "application/json" } });
    }));
    render(<PushPrefs />);
    expect(await screen.findByText(/Ask an Admin to turn it on/)).toBeTruthy();
    cleanup();
    render(<PushPrefs admin />);
    fireEvent.click(await screen.findByRole("button", { name: "Turn on web push for this server" }));
    expect(await screen.findByText(/Web push is on for this server/)).toBeTruthy();
    expect(calls.find((c) => c.method === "PUT")).toMatchObject({ url: "/api/v1/settings/notifications.push.enabled", body: '{"value":true}' });
  });

  it("colours the incident name by severity unless turned off", async () => {
    const { SeverityColorPrefs } = await import("./AppearancePanel");
    const { paintSevCells, sevClass } = await import("../lib/sevcells");
    localStorage.removeItem("forgesre.sevcells");
    paintSevCells();
    expect(document.documentElement.dataset.sevcells).toBe("on");
    expect(sevClass("CRITICAL")).toBe("sev-cell sev-critical");
    expect(sevClass("info")).toBe("sev-cell");
    render(<SeverityColorPrefs />);
    fireEvent.click(screen.getByLabelText("Colour the incident name by severity"));
    expect(document.documentElement.dataset.sevcells).toBe("off");
    expect(localStorage.getItem("forgesre.sevcells")).toBe("off");
  });

  it("shows push when it is on", async () => {
    stub({ enabled: true, public_key: "BAAA", devices: 0 });
    render(<PushPrefs />);
    expect(await screen.findByText("Push to this device")).toBeTruthy();
  });

  it("a Settings group from Ctrl+K reaches an open Settings page", () => {
    stub({ incidents: [], assets: [] });
    const seen: string[] = [];
    const listen = (e: Event) => seen.push(String((e as CustomEvent<string>).detail));
    window.addEventListener("forgesre-settings-group", listen);
    render(<MemoryRouter><CommandPalette /></MemoryRouter>);
    fireEvent.keyDown(window, { key: "k", ctrlKey: true });
    const box = screen.getByRole("combobox");
    fireEvent.change(box, { target: { value: "synthetic" } });
    fireEvent.keyDown(box, { key: "Enter" });
    window.removeEventListener("forgesre-settings-group", listen);
    expect(seen).toEqual(["checks"]);
  });
});
