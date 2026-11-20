import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { ForgeAiSkills, type Skill } from "./ForgeAiSkills";

const disk: Skill = {
  slug: "linux-disk", name: "Linux disk full", task: "incident", task_label: "Cause and first checks", match_alerts: ["*disk*"], match_types: ["Linux Server"],
  priority: 100, instructions: "A filesystem is filling.", default_instructions: "A filesystem is filling.", include: { facts: true, playbook: true, similar: true, changes: true, syslog: false },
  enabled: true, builtin: true, version: 3, updated_by: "ana@dc.local", updated_at: null,
  history: [{ version: 2, instructions: "Older text about disks.", by: "marko", at: null }], stats: { calls: 5, used: 4, rejected: 1, errors: 0, up: 3, down: 0, avg_s: 41.2 },
};

describe("ForgeAiSkills", () => {
  afterEach(() => (cleanup(), vi.unstubAllGlobals()));

  it("edits a skill, tries the unsaved text and saves a new version", async () => {
    const calls: { url: string; method: string; body?: string }[] = [];
    vi.stubGlobal("fetch", vi.fn(async (url: string, init?: RequestInit) => {
      calls.push({ url, method: init?.method ?? "GET", body: init?.body as string | undefined });
      const body = url === "/api/v1/forgeai/skills" && !init?.method ? { skills: [disk], tasks: { incident: "Cause and first checks" }, can_edit: true }
        : url === "/api/v1/forgeai/trial" ? { job: 4 }
        : url === "/api/v1/forgeai/jobs/4" ? { status: "done", error: "", call_id: 11 }
        : url.endsWith("/tuning") ? { skill: "linux-disk", task: "incident", version: 3, days: 60, calls: 0, used: 0, rejected: 0, errors: 0, thumbs_up: 0, thumbs_down: 0, ratings: {}, correct_share: null, reject_reasons: [], error_reasons: [], examples: [], replay_candidates: 0 }
        : url === "/api/v1/forgeai/calls/11" ? { id: 11, at: null, task: "incident", skill: "linux-disk", skill_version: 3, model: "qwen", status: "trial", reason: "", duration_ms: 30000, answer: { cause: "Archive logs filled /u02." }, rating: 0, actor: "x", system_prompt: "rules", user_prompt: "{}", raw_response: "{}" }
        : { ...disk, version: 4 };
      return new Response(JSON.stringify(body), { status: 200, headers: { "Content-Type": "application/json" } });
    }));
    render(<ForgeAiSkills />);
    expect(await screen.findByText("Linux disk full · v3")).toBeTruthy();
    expect(screen.getAllByText(/5 questions · 4 used · 1 rejected/).length).toBeGreaterThan(0);
    const box = screen.getByLabelText(/Instructions to ForgeAI/);
    fireEvent.change(box, { target: { value: "Check the archive log purge first." } });
    fireEvent.click(await screen.findByRole("button", { name: "Try the unsaved text" }));
    expect(await screen.findByText(/Archive logs filled/)).toBeTruthy();
    expect(JSON.parse(calls.find((c) => c.url === "/api/v1/forgeai/trial")!.body!)).toMatchObject({ skill: "linux-disk", instructions: "Check the archive log purge first." });
    fireEvent.click(screen.getByRole("button", { name: "Save as v4" }));
    await waitFor(() => expect(calls.some((c) => c.method === "PUT" && c.url === "/api/v1/forgeai/skills/linux-disk")).toBe(true));
    fireEvent.click(screen.getByText("Earlier versions (1)"));
    expect(screen.getByText("Older text about disks.")).toBeTruthy();
  });
});
