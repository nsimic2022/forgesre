import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { SyntheticChecks } from "./SyntheticChecks";

describe("Synthetic checks", () => {
  afterEach(() => (cleanup(), vi.unstubAllGlobals()));

  it("lists results and adds a check", async () => {
    const posts: unknown[] = [];
    vi.stubGlobal("fetch", vi.fn(async (_url: string, init?: RequestInit) => {
      if (init?.method === "POST") posts.push(JSON.parse(String(init.body)));
      const body = init?.method === "POST" ? { id: 2 } : { error: "", can_edit: true, checks: [
        { id: 1, name: "erp-web", kind: "http", target: "https://erp/health", asset: "erp-01", severity: "critical", enabled: true, up: 0, seconds: 0.21 },
      ] };
      return new Response(JSON.stringify(body), { status: 200, headers: { "Content-Type": "application/json" } });
    }));
    render(<SyntheticChecks />);
    expect(await screen.findByText("failing")).toBeTruthy();
    expect(screen.getByText("210 ms")).toBeTruthy();
    fireEvent.change(screen.getByPlaceholderText("erp-web"), { target: { value: "dns-1" } });
    fireEvent.change(screen.getByRole("combobox", { name: "Kind" }), { target: { value: "dns" } });
    fireEvent.change(screen.getByPlaceholderText("10.0.0.53"), { target: { value: "10.0.0.53" } });
    fireEvent.click(screen.getByRole("button", { name: "Add check" }));
    await screen.findByText("failing");
    await vi.waitFor(() => expect(posts).toEqual([{ name: "dns-1", kind: "dns", target: "10.0.0.53", asset: "", severity: "critical" }]));
  });
});
