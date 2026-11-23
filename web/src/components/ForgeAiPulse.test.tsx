import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { ForgeAiChat } from "./ForgeAiChat";
import { ForgeAiPulse, type Pulse } from "./ForgeAiPulse";

const base: Pulse = {
  local: { on: true, model: "qwen3-4b", reachable: true, why: "" },
  test: { status: "ok", ms: 4200, first_ms: 900, at: null },
  lane: { pending: 1, running: { id: 3, kind: "forgeai_chat", seconds: 12 } },
  worker_age_s: 5, timeout_s: 90, thinking: true,
  cloud: { enabled: false, ready: false, provider: "", model: "" },
  can_test: true,
};

function stub(pulse: Pulse, extra: Record<string, unknown> = {}) {
  const posts: string[] = [];
  vi.stubGlobal("fetch", vi.fn(async (url: string, init?: RequestInit) => {
    if (init?.method === "POST") posts.push(url);
    const body = url === "/api/v1/forgeai/pulse" ? pulse : url === "/api/v1/me" ? { email: "a@dc.local", role: "analyst", name: "A" } : extra[url] ?? {};
    return new Response(JSON.stringify(body), { status: 200, headers: { "Content-Type": "application/json" } });
  }));
  return posts;
}

describe("ForgeAI status line and progress", () => {
  afterEach(() => (cleanup(), vi.unstubAllGlobals()));

  it("shows model, connection, test, local and queue, and runs a test", async () => {
    const posts = stub(base);
    render(<ForgeAiPulse />);
    expect(await screen.findByText("qwen3-4b")).toBeTruthy();
    expect(screen.getByText("ok · 4.2 s")).toBeTruthy();
    expect(screen.getByText("local")).toBeTruthy();
    expect(screen.getByText("busy 12 s · 1 waiting")).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "Test the model now" }));
    await waitFor(() => expect(posts).toContain("/api/v1/forgeai/ping"));
  });

  it("says when the model does not answer and why the test failed", async () => {
    stub({ ...base, local: { ...base.local, reachable: false, why: "connection refused" }, test: { status: "error", error: "no complete answer within 90 s" } });
    render(<ForgeAiPulse />);
    expect(await screen.findByText("down")).toBeTruthy();
    expect(screen.getByText("no complete answer within 90 s")).toBeTruthy();
  });

  it("shows where a pending answer is and folds the reasoning", async () => {
    const started = new Date(Date.now() - 20000).toISOString();
    stub(base, {
      "/api/v1/forgeai/chats/5": {
        id: 5, title: "t", incident: null, incident_label: "", updated_at: null, skills: [],
        messages: [
          { id: 1, role: "user", text: "why?", skill: "", cloud: false, status: "done", call_id: null, at: null },
          { id: 2, role: "assistant", text: "", skill: "", cloud: false, status: "pending", call_id: null, at: null, phase: "thinking",
            thinking: "The disk filled because the archive job stopped", started_at: started, queue: { ahead: 0, running: null, timeout_s: 90 } },
        ],
      },
    });
    render(<ForgeAiChat chatId={5} />);
    expect(await screen.findByText("Thinking")).toBeTruthy();
    expect(screen.getByText(/of max 1:30/)).toBeTruthy();
    expect(screen.getByText("Show what it is thinking")).toBeTruthy();
    expect(screen.getByText(/archive job stopped/)).toBeTruthy();
  });
});
