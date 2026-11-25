import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { CompareModels, GoldenSet } from "./GoldenSet";

describe("Golden set", () => {
  afterEach(() => (cleanup(), vi.unstubAllGlobals()));

  it("shows cases with the last result and the run score", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => new Response(JSON.stringify({
      running: false, can_edit: true,
      cases: [{ id: 1, incident: "INC-0007_x", title: "Disk full", cause: "Archive logs not purged", expect: ["archive", "purged"] }],
      runs: [{ id: 3, started_at: null, finished_at: "2026-11-23T10:00:00Z", model: "qwen3-4b", skill: "best match", language: "sr", passed: 1, total: 1,
        results: [{ incident: "INC-0007", passed: true, why: "names 'archive'", cause: "Archive logs filled /u02", ms: 41000, skill: "linux-disk" }] }],
    }), { status: 200, headers: { "Content-Type": "application/json" } })));
    render(<GoldenSet />);
    expect(await screen.findByText("Archive logs not purged")).toBeTruthy();
    expect(screen.getByText("pass")).toBeTruthy();
    expect(screen.getByText("1/1")).toBeTruthy();
    expect(screen.getByRole("button", { name: "Run golden set" })).toBeTruthy();
  });

  it("Compare two models posts both models and shows them side by side", async () => {
    const posts: unknown[] = [];
    vi.stubGlobal("fetch", vi.fn(async (_url: string, init?: RequestInit) => {
      if (init?.method === "POST") posts.push(JSON.parse(String(init.body)));
      return new Response(JSON.stringify({ models: [{ name: "q.gguf", label: "Qwen3 8B", size_gb: 5, active: true }, { name: "o.gguf", label: "OpsLLM 7B", size_gb: 4, active: false }], job: 9 }),
        { status: 200, headers: { "Content-Type": "application/json" } });
    }));
    const run = (id: number, model: string, passed: boolean) => ({ id, started_at: null, finished_at: null, model, skill: "", language: "en", passed: passed ? 1 : 0, total: 1, rejected: passed ? 0 : 1, avg_ms: 2500,
      results: [{ incident: "INC-0007", passed, why: passed ? "names 'raid'" : "missing 'raid'", cause: "", ms: 2500, skill: "" }] });
    const started = vi.fn();
    render(<CompareModels cases={[{ id: 1, incident: "INC-0007", title: "", cause: "", expect: [] }]} canEdit busy={false}
      comparisons={[{ id: "c1", runs: [run(1, "Qwen3 8B", true), run(2, "OpsLLM 7B", false)] }]} onStarted={started} />);
    expect(await screen.findByText("A: Qwen3 8B")).toBeTruthy();
    expect((screen.getByLabelText("Model B") as HTMLSelectElement).value).toBe("o.gguf");
    fireEvent.click(screen.getByRole("button", { name: "Compare" }));
    await vi.waitFor(() => expect(posts[0]).toEqual({ a: "q.gguf", b: "o.gguf" }));
    await vi.waitFor(() => expect(started).toHaveBeenCalled());
    const table = screen.getByRole("table", { name: "Model comparison" });
    expect(table.textContent).toContain("rejected 1 · avg 2.5 s");
    expect(table.textContent).toContain("missing 'raid'");
  });
});
