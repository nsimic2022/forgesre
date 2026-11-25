import { cleanup, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { ModelDetails } from "./ModelDetails";

describe("Model details", () => {
  afterEach(() => (cleanup(), vi.unstubAllGlobals()));

  it("shows the chat model file, how it runs, and the document-search model", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => new Response(JSON.stringify({
      chat: { file: "OpsLLM-7B.i1-Q4_K_M.gguf", label: "OpsLLM 7B (i1-Q4_K_M)", size_gb: 4.7, context: "8192", threads: "8", gpu_layers: "99", gpu: false,
        url: "http://127.0.0.1:8088/v1", mode: "local", thinking: false, answer_percent: 100, think_tokens: 1024, timeout_s: 180 },
      embed: { files: [{ name: "nomic-embed-text-v1.5.Q8_0.gguf", size_mb: 146 }], model: "nomic-embed-text-v1.5", url: "http://127.0.0.1:8089/v1", threads: "2" },
      dir: "/models", answer_lengths: [{ task: "chat", label: "Chat", base: 800, now: 800 }],
    }), { status: 200, headers: { "Content-Type": "application/json" } })));
    render(<ModelDetails />);
    expect(await screen.findByText("OpsLLM 7B (i1-Q4_K_M)")).toBeTruthy();
    expect(screen.getByText("8192 tokens")).toBeTruthy();
    expect(screen.getByText(/nomic-embed-text-v1.5.Q8_0.gguf · 146 MB/)).toBeTruthy();
    expect(screen.getAllByText("off").length).toBe(2); // GPU and thinking
  });
});
