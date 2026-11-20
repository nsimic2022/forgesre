import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { ForgeAiModels } from "./ForgeAiModels";

const data = {
  dir: "/host-models", writable: true, disk_free_gb: 40, ram_gb: 15.6, active: "model.gguf",
  installed: [
    { name: "model.gguf", size_gb: 2.5, parts: 1, complete: true, valid: true, active: true, catalog: "" },
    { name: "Qwen3-8B-Q4_K_M.gguf", size_gb: 5, parts: 1, complete: true, valid: true, active: false, catalog: "qwen3-8b" },
  ],
  catalog: [
    { id: "qwen3-8b", name: "Qwen3 8B (Q4_K_M)", size_gb: 5, ram_gb: 10, timeout_s: 180, note: "Better.", files: [] },
    { id: "qwen2.5-14b", name: "Qwen2.5 14B Instruct (Q4_K_M)", size_gb: 9, ram_gb: 16, timeout_s: 600, note: "Slow.", files: [] },
  ],
  downloads: [],
};

describe("ForgeAiModels", () => {
  afterEach(() => (cleanup(), vi.unstubAllGlobals()));

  it("lists models, warns about RAM, switches the active model and starts a download", async () => {
    const posts: { url: string; body: string }[] = [];
    vi.stubGlobal("fetch", vi.fn(async (url: string, init?: RequestInit) => {
      if (init?.method === "POST") posts.push({ url, body: String(init.body) });
      const body = url === "/api/v1/llm/models" ? data : url.endsWith("/activate") ? { active: "Qwen3-8B-Q4_K_M.gguf", timeout_s: 180 } : { queued: true, job: 3 };
      return new Response(JSON.stringify(body), { status: 200, headers: { "Content-Type": "application/json" } });
    }));
    render(<ForgeAiModels />);
    expect(await screen.findByText("Active")).toBeTruthy();
    expect(screen.getByText("Installed")).toBeTruthy();
    expect(screen.getByText("More RAM than this VM has.")).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "Use this model" }));
    await waitFor(() => expect(posts.some((p) => p.url === "/api/v1/llm/models/Qwen3-8B-Q4_K_M.gguf/activate")).toBe(true));
    fireEvent.click(screen.getByRole("button", { name: "Download" }));
    await waitFor(() => expect(posts.some((p) => p.url === "/api/v1/llm/models/download" && p.body === '{"catalog_id":"qwen2.5-14b"}')).toBe(true));
  });
});
