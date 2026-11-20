import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { afterEach, describe, expect, it, vi } from "vitest";
import { DemoPanel } from "./DemoPanel";

describe("DemoPanel", () => {
  afterEach(() => (cleanup(), vi.unstubAllGlobals()));

  it("runs a scenario as a job and links the incident it opened", async () => {
    const posts: string[] = [];
    vi.stubGlobal("fetch", vi.fn(async (url: string, init?: RequestInit) => {
      if (init?.method === "POST") posts.push(`${url} ${String(init.body)}`);
      const body = url === "/api/v1/demo/scenarios" ? { scenarios: [{ key: "highcpu", label: "High CPU", detail: "HighCPU on forge-demo-01" }] }
        : url === "/api/v1/demo/run" ? { job: 5 }
        : url === "/api/v1/demo-reset" ? { ok: true }
        : { status: "done", error: "", result: { incident: "INC-0007" } };
      return new Response(JSON.stringify(body), { status: 200, headers: { "Content-Type": "application/json" } });
    }));
    render(<MemoryRouter><DemoPanel /></MemoryRouter>);
    fireEvent.click(await screen.findByRole("button", { name: "Run" }));
    expect((await screen.findByRole("link", { name: "INC-0007" })).getAttribute("href")).toBe("/incidents/INC-0007");
    expect(posts[0]).toBe('/api/v1/demo/run {"scenario":"highcpu"}');
    fireEvent.click(screen.getByRole("button", { name: "Lower demo gauges" }));
    expect(await screen.findByText(/gauges lowered/)).toBeTruthy();
  });
});
