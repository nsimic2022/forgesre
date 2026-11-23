import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { ForgeAiFound } from "./AskForgeAi";

const auto = { id: 1, at: null, task: "incident", skill: "linux-disk", skill_version: 1, model: "qwen", status: "used", reason: "", duration_ms: 41000, rating: 0, actor: "system",
  answer: { cause: "Archive logs filled /u02", first_checks: ["Check archive purge"], for_shift_lead: "Disk full on db-01" }, system_prompt: "rules", user_prompt: "{}", raw_response: "{}" };

describe("What ForgeAI found on its own", () => {
  afterEach(() => (cleanup(), vi.unstubAllGlobals()));

  it("shows the automatic answer", async () => {
    const posts: { url: string; body: unknown }[] = [];
    vi.stubGlobal("fetch", vi.fn(async (url: string, init?: RequestInit) => {
      if (init?.method === "POST") posts.push({ url, body: JSON.parse(String(init.body)) });
      const body =
        url === "/api/v1/me" ? { email: "e@dc.local", role: "engineer", name: "E" }
        : url === "/api/v1/forgeai/chats" ? { id: 4, title: "INC-0001 Disk full", incident: "INC-0001", incident_label: "INC-0001 Disk full", messages: [] }
        : url === "/api/v1/forgeai/chats/4" ? { id: 4, title: "INC-0001 Disk full", incident: "INC-0001", incident_label: "INC-0001 Disk full", updated_at: null, messages: [], skills: [] }
        : url.includes("/forgeai") && url.startsWith("/api/v1/incidents/") ? { calls: [auto] }
        : {};
      return new Response(JSON.stringify(body), { status: 200, headers: { "Content-Type": "application/json" } });
    }));
    render(<ForgeAiFound number="INC-0001" />);
    expect(await screen.findByText("Archive logs filled /u02")).toBeTruthy();
    expect(posts).toEqual([]);
  });

  it("asks the cloud model for a second opinion and shows its verdict", async () => {
    const posts: string[] = [];
    let calls: unknown[] = [auto];
    const critic = { ...auto, id: 5, task: "critic", status: "trial", model: "gpt-test", answer: { verdict: "partly", why: "The second check is vague.", problems: ["check is vague"], reviews_call: 1 } };
    vi.stubGlobal("fetch", vi.fn(async (url: string, init?: RequestInit) => {
      if (init?.method === "POST") posts.push(url);
      const body =
        url === "/api/v1/me" ? { email: "e@dc.local", role: "engineer", name: "E" }
        : url === "/api/v1/forgeai/status" ? { cloud: { enabled: true, ready: true, why: "", provider: "openai", model: "gpt-test", host: "api.openai.com", can_use: true } }
        : url === "/api/v1/forgeai/skills" ? { skills: [] }
        : url.endsWith("/critique") ? ((calls = [critic, auto]), { job: 11 })
        : url === "/api/v1/forgeai/jobs/11" ? { status: "done", error: "", call_id: 5 }
        : { calls };
      return new Response(JSON.stringify(body), { status: 200, headers: { "Content-Type": "application/json" } });
    }));
    render(<ForgeAiFound number="INC-0003" />);
    fireEvent.click(await screen.findByRole("button", { name: "Second opinion (openai)" }));
    expect(await screen.findByText("The second check is vague.")).toBeTruthy();
    expect(posts).toContain("/api/v1/forgeai/calls/1/critique");
    expect(screen.queryByRole("button", { name: "Second opinion (openai)" })).toBeNull();
  });
});
