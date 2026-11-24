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

  it("hides push when the Admin has not turned it on", async () => {
    stub({ enabled: false, public_key: "", devices: 0 });
    const { container } = render(<PushPrefs />);
    await new Promise((r) => setTimeout(r, 20));
    expect(container.textContent).toBe("");
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
