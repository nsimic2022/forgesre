import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { AskForgeAi } from "./AskForgeAi";

const auto = { id: 1, at: null, task: "incident", skill: "linux-disk", skill_version: 1, model: "qwen", status: "used", reason: "", duration_ms: 41000, rating: 0, actor: "system",
  answer: { cause: "Archive logs filled /u02", first_checks: ["Check archive purge"], for_shift_lead: "Disk full on db-01" }, system_prompt: "rules", user_prompt: "{}", raw_response: "{}" };
const trial = { ...auto, id: 2, skill: "database", status: "trial", actor: "ana@dc.local", answer: { cause: "Long session wrote temp files", first_checks: ["Check sessions"], for_shift_lead: "x" } };
const asked = { ...auto, id: 3, task: "ask", skill: "ask-generic", status: "trial", actor: "ana@dc.local", user_prompt: '{"operator_question": "was it the backup?"', answer: { answer: "The facts show no backup before the alarm.", not_in_facts: ["backup schedule"] } };

describe("Ask ForgeAI", () => {
  afterEach(() => (cleanup(), vi.unstubAllGlobals()));

  it("shows the automatic answer, compares a skill, asks a question, and lets an Engineer use the trial", async () => {
    const posts: { url: string; body: unknown }[] = [];
    let calls: unknown[] = [auto];
    vi.stubGlobal("fetch", vi.fn(async (url: string, init?: RequestInit) => {
      if (init?.method === "POST") posts.push({ url, body: JSON.parse(String(init.body)) });
      const body =
        url === "/api/v1/me" ? { email: "e@dc.local", role: "engineer", name: "E" }
        : url === "/api/v1/forgeai/skills" ? { skills: [{ slug: "database", name: "Database", task: "incident", enabled: true }, { slug: "ask-generic", name: "Operator question", task: "ask", enabled: true }] }
        : url.endsWith("/forgeai/ask") ? ((calls = (posts.at(-1)!.body as { question: string }).question ? [asked, trial, auto] : [trial, auto]), { job: 7 })
        : url === "/api/v1/forgeai/jobs/7" ? { status: "done", error: "", call_id: (posts.at(-1)!.body as { question: string }).question ? 3 : 2 }
        : url.endsWith("/use") ? { id: 2, status: "used" }
        : url.includes("/forgeai") ? { calls }
        : {};
      return new Response(JSON.stringify(body), { status: 200, headers: { "Content-Type": "application/json" } });
    }));
    render(<AskForgeAi number="INC-0001" />);
    expect(await screen.findByText("Archive logs filled /u02")).toBeTruthy();
    fireEvent.change(await screen.findByLabelText("Skill to compare"), { target: { value: "database" } });
    fireEvent.click(screen.getByRole("button", { name: "Ask with this skill" }));
    expect((await screen.findAllByText("Long session wrote temp files")).length).toBeGreaterThan(0);
    fireEvent.click(screen.getByRole("button", { name: "Use this answer" }));
    await waitFor(() => expect(posts.some((p) => p.url === "/api/v1/forgeai/calls/2/use")).toBe(true));
    fireEvent.change(screen.getByLabelText("Your question"), { target: { value: "was it the backup?" } });
    fireEvent.click(screen.getByRole("button", { name: "Ask ForgeAI" }));
    expect((await screen.findAllByText("The facts show no backup before the alarm.")).length).toBeGreaterThan(0);
    expect(screen.getByText(/“was it the backup\?”/)).toBeTruthy();
  });
});
