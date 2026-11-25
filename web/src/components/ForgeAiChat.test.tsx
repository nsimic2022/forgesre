import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { ForgeAiChat } from "./ForgeAiChat";

const skills = [
  { slug: "chat-generic", name: "Chat", task: "chat", task_label: "Chat" },
  { slug: "linux-disk", name: "Linux disk full", task: "incident", task_label: "Cause and first checks" },
  { slug: "network-link", name: "Network link or interface", task: "incident", task_label: "Cause and first checks" },
];

function stub(opts: { cloud?: boolean } = {}) {
  const posts: { url: string; body: Record<string, unknown> }[] = [];
  let messages: unknown[] = [];
  vi.stubGlobal("fetch", vi.fn(async (url: string, init?: RequestInit) => {
    if (init?.method === "POST") {
      const body = JSON.parse(String(init.body));
      posts.push({ url, body });
      if (url.endsWith("/messages")) {
        messages = [
          { id: 1, role: "user", text: body.text || `/${body.skill}`, skill: body.skill, cloud: body.cloud, status: "done", call_id: null, at: null },
          { id: 2, role: "assistant", text: "Cause: archive logs.\nFirst checks:\n1. Purge job", skill: body.skill, cloud: body.cloud, status: "done", call_id: 9, at: null },
        ];
      }
    }
    const body =
      url === "/api/v1/me" ? { email: "e@dc.local", role: "engineer", name: "E" }
      : url === "/api/v1/forgeai/status" ? { cloud: { enabled: Boolean(opts.cloud), ready: true, provider: "anthropic", model: "claude-test", host: "api.anthropic.com", can_use: true } }
      : url === "/api/v1/forgeai/chats/3" ? { id: 3, title: "INC-0007 Disk full", incident: "INC-0007", incident_label: "INC-0007 Disk full", updated_at: null, messages, skills }
      : {};
    return new Response(JSON.stringify(body), { status: 200, headers: { "Content-Type": "application/json" } });
  }));
  return posts;
}

describe("ForgeAI chat", () => {
  afterEach(() => (cleanup(), vi.unstubAllGlobals()));

  it("sends a message with Enter and shows the answer with line breaks", async () => {
    const posts = stub();
    render(<ForgeAiChat chatId={3} />);
    const box = await screen.findByLabelText("Message to ForgeAI");
    expect(screen.getByText("Ask about INC-0007 Disk full")).toBeTruthy();
    fireEvent.change(box, { target: { value: "was it the backup?" } });
    fireEvent.keyDown(box, { key: "Enter" });
    expect(await screen.findByText(/First checks:\s+1\. Purge job/)).toBeTruthy();
    expect(posts[0]).toEqual({ url: "/api/v1/forgeai/chats/3/messages", body: { text: "was it the backup?", skill: "", cloud: false } });
    expect(screen.getByText(/nothing leaves this server/)).toBeTruthy();
  });

  it("opens the skill menu with / and filters it, then sends the skill", async () => {
    const posts = stub();
    render(<ForgeAiChat chatId={3} />);
    const box = await screen.findByLabelText("Message to ForgeAI");
    fireEvent.change(box, { target: { value: "/net" } });
    const menu = await screen.findByRole("listbox", { name: "Skills" });
    expect(menu.textContent).toContain("Network link or interface");
    expect(menu.textContent).not.toContain("Linux disk full");
    fireEvent.keyDown(box, { key: "Enter" });
    expect(screen.getByText(/Cause and first checks: Network link or interface/)).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "Send" }));
    await waitFor(() => expect(posts[0]?.body).toEqual({ text: "", skill: "network-link", cloud: false }));
    expect(await screen.findByRole("button", { name: "Use as the incident's answer" })).toBeTruthy();
  });

  it("offers the cloud model only when ready and says where the message goes", async () => {
    const posts = stub({ cloud: true });
    render(<ForgeAiChat chatId={3} />);
    fireEvent.click(await screen.findByRole("button", { name: "Cloud" }));
    expect(screen.getByText(/goes to anthropic · claude-test/)).toBeTruthy();
    fireEvent.change(screen.getByLabelText("Message to ForgeAI"), { target: { value: "why?" } });
    fireEvent.click(screen.getByRole("button", { name: "Send" }));
    await waitFor(() => expect(posts[0]?.body.cloud).toBe(true));
  });
});

describe("chat mode", () => {
  it("shows how strictly ForgeAI answers and changes it for this chat", async () => {
    const { ChatModePicker } = await import("./ForgeAiChat");
    const { render, screen, fireEvent, cleanup } = await import("@testing-library/react");
    const calls: string[] = [];
    vi.stubGlobal("fetch", vi.fn(async (url: string, init?: RequestInit) => {
      calls.push(`${init?.method} ${url} ${init?.body}`);
      return new Response(JSON.stringify({ id: 3, mode: "open", mode_effective: "open" }), { status: 200, headers: { "Content-Type": "application/json" } });
    }));
    const changed = vi.fn();
    const chat = { id: 3, title: "t", incident: "INC-1", incident_label: "INC-1 disk", updated_at: null, messages: [], skills: [], mode: "" as const, mode_effective: "balanced" as const };
    render(<ChatModePicker chat={chat} onChanged={changed} />);
    expect(screen.getByRole("button", { name: "Balanced" }).getAttribute("aria-pressed")).toBe("true");
    expect(screen.getByText(/general knowledge, marked · the default/)).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "Open" }));
    await vi.waitFor(() => expect(changed).toHaveBeenCalled());
    expect(calls[0]).toBe('POST /api/v1/forgeai/chats/3/mode {"mode":"open"}');
    cleanup();
    render(<ChatModePicker chat={{ ...chat, incident: null, mode_effective: "general" }} onChanged={changed} />);
    expect(screen.getByText("general knowledge")).toBeTruthy();
    vi.unstubAllGlobals();
  });
});

describe("chat model picker", () => {
  it("lists downloaded and imported models and switches for an Engineer", async () => {
    const { ChatModelPicker } = await import("./ForgeAiChat");
    const { render, screen, fireEvent, cleanup } = await import("@testing-library/react");
    const calls: string[] = [];
    vi.stubGlobal("fetch", vi.fn(async (url: string, init?: RequestInit) => {
      calls.push(`${init?.method ?? "GET"} ${url} ${init?.body ?? ""}`);
      const body = url.endsWith("/switch") ? { active: "OpsLLM-7B.i1-Q4_K_M.gguf", timeout_s: 180 } : { active: "Qwen3-8B-Q4_K_M.gguf", can_switch: true, mode: "local", models: [
        { name: "Qwen3-8B-Q4_K_M.gguf", label: "Qwen3 8B (Q4_K_M)", size_gb: 5, active: true, source: "catalog" },
        { name: "OpsLLM-7B.i1-Q4_K_M.gguf", label: "OpsLLM 7B (i1-Q4_K_M)", size_gb: 4.7, active: false, source: "catalog" },
        { name: "mine.gguf", label: "mine", size_gb: 3, active: false, source: "imported" }] };
      return new Response(JSON.stringify(body), { status: 200, headers: { "Content-Type": "application/json" } });
    }));
    vi.stubGlobal("confirm", () => true);
    render(<ChatModelPicker />);
    const select = (await screen.findByLabelText("ForgeAI model")) as HTMLSelectElement;
    expect([...select.options].map((o) => o.textContent)).toEqual(["Qwen3 8B (Q4_K_M) · 5 GB", "OpsLLM 7B (i1-Q4_K_M) · 4.7 GB", "mine · 3 GB · imported"]);
    fireEvent.change(select, { target: { value: "OpsLLM-7B.i1-Q4_K_M.gguf" } });
    expect(await screen.findByText(/Switched. The model is loading/)).toBeTruthy();
    expect(calls).toContain('POST /api/v1/forgeai/models/switch {"name":"OpsLLM-7B.i1-Q4_K_M.gguf"}');
    cleanup();
    vi.unstubAllGlobals();
  });
});
