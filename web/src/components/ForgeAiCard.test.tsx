import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { ForgeAiCard, type ForgeAiView } from "./ForgeAiCard";

const view: ForgeAiView = {
  mode: "local", text: "", cause: "", action: "", model: "qwen3-4b", at: null, skill: "Network link or interface", call_id: 7,
  answer: { cause: "The uplink port went down.", first_checks: ["Check the far end.", "Look at port errors."], for_shift_lead: "One access switch lost a link." },
  skills: [{ slug: "incident-generic", name: "Any incident" }, { slug: "network-link", name: "Network link or interface" }],
  extras: { explain: { answer: { explanation: "A switch port stopped passing traffic.", why_it_matters: "Users behind it are offline." }, call_id: 8 } },
};
const call = { id: 7, at: null, task: "incident", skill: "network-link", skill_version: 2, model: "qwen3-4b", status: "used", reason: "", duration_ms: 41000,
  answer: {}, rating: 0, actor: "system", system_prompt: "You are ForgeAI…", user_prompt: '{"incident":{"title":"Interface down"}}', raw_response: '{"cause":"The uplink port went down."}' };

describe("ForgeAiCard", () => {
  afterEach(() => (cleanup(), vi.unstubAllGlobals()));

  it("shows the checked answer, rates it, asks again with a chosen skill and shows the exact prompts", async () => {
    const posts: { url: string; body: string }[] = [];
    vi.stubGlobal("fetch", vi.fn(async (url: string, init?: RequestInit) => {
      if (init?.method === "POST") posts.push({ url, body: String(init.body) });
      const body = url.endsWith("/rate") ? { rating: 1, stars: 4 } : url.endsWith("/forgeai") && init?.method === "POST" ? { job: 5 }
        : url === "/api/v1/forgeai/jobs/5" ? { status: "done", error: "", call_id: 9 } : { calls: [call] };
      return new Response(JSON.stringify(body), { status: 200, headers: { "Content-Type": "application/json" } });
    }));
    const changed = vi.fn();
    render(<ForgeAiCard number="INC-0412" view={view} canAsk onChanged={changed} />);
    expect(screen.getByText("The uplink port went down.")).toBeTruthy();
    expect(screen.getByText("Look at port errors.")).toBeTruthy();
    expect(screen.getByText("A switch port stopped passing traffic.")).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: /^4 of 5/ }));
    await waitFor(() => expect(posts.some((p) => p.url === "/api/v1/forgeai/calls/7/rate" && p.body === '{"stars":4}')).toBe(true));
    fireEvent.change(screen.getByLabelText("Skill"), { target: { value: "incident-generic" } });
    fireEvent.click(screen.getByRole("button", { name: "Ask again" }));
    await waitFor(() => expect(changed).toHaveBeenCalled());
    expect(posts.find((p) => p.url === "/api/v1/incidents/INC-0412/forgeai")!.body).toBe('{"task":"incident","skill":"incident-generic"}');
    fireEvent.click(screen.getByRole("button", { name: "What was ForgeAI asked?" }));
    expect(await screen.findByText(/skill network-link v2/)).toBeTruthy();
    expect(screen.getByText("You are ForgeAI…")).toBeTruthy();
  });
});

describe("ForgeAiCard investigation", () => {
  it("shows the investigation's cause with the evidence it cited", () => {
    const deep: ForgeAiView = {
      ...view,
      deep: {
        call_id: 9, status: "used", reason: "", at: null, model: "qwen3-4b", seconds: 95, evidence_count: 7,
        answer: { cause: "Backups filled /data.", confidence: "medium", evidence: ["E3"], next_steps: ["Check the backup job."] },
        cited: [{ id: "E3", source: "metric", text: "disk_percent around the alarm: max 97.0" }],
      },
    };
    render(<ForgeAiCard number="INC-0412" view={deep} canAsk onChanged={() => undefined} />);
    expect(screen.getByText("Backups filled /data.")).toBeTruthy();
    expect(screen.getByText("disk_percent around the alarm: max 97.0")).toBeTruthy();
    expect(screen.getByRole("button", { name: "Investigate with tools" })).toBeTruthy();
  });
});
