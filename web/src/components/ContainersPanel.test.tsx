import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { ContainersPanel } from "./ContainersPanel";

describe("Containers", () => {
  afterEach(() => (cleanup(), vi.unstubAllGlobals()));

  it("lists containers, flags a stopped one and shows its log with an errors-only filter", async () => {
    vi.stubGlobal("fetch", vi.fn(async (url: string) => new Response(JSON.stringify(url.includes("/logs")
      ? { service: "librenms", name: "forgesre-librenms-1", state: "exited", status: "Exited (1)", tail: 200, lines: ["10:00 started", "10:01 ERROR db refused password=•••"] }
      : { error: "", containers: [
        { id: "b", name: "forgesre-librenms-1", service: "librenms", image: "librenms/librenms", state: "exited", status: "Exited (1) 5 minutes ago", health: "" },
        { id: "a", name: "forgesre-core-1", service: "core", image: "forgesre-core", state: "running", status: "Up 2 hours (healthy)", health: "healthy" }] }),
      { status: 200, headers: { "Content-Type": "application/json" } })));
    render(<ContainersPanel />);
    expect(await screen.findByText("1 not running")).toBeTruthy();
    expect(screen.getByText("exited")).toBeTruthy();
    fireEvent.click(screen.getAllByRole("button", { name: "Logs" })[0]);
    expect(await screen.findByText("Logs · librenms")).toBeTruthy();
    expect(await screen.findByText(/10:00 started/)).toBeTruthy();
    fireEvent.click(screen.getByLabelText("Errors only"));
    expect(screen.queryByText(/10:00 started/)).toBeNull();
    expect(screen.getByText(/ERROR db refused/)).toBeTruthy();
  });
});
