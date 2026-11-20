import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { ForgeAiTuning } from "./ForgeAiTuning";

const report = {
  skill: "linux-disk", task: "incident", version: 3, days: 60, calls: 9, used: 6, rejected: 2, errors: 1, thumbs_up: 2, thumbs_down: 1,
  ratings: { correct: 3, wrong: 1 }, correct_share: 0.75, reject_reasons: [{ reason: "the answer has a number that is not in the facts", count: 2 }], error_reasons: [],
  examples: [{ incident: "INC-0007", title: "Disk full", said: "CPU was high", confirmed: "Backups filled /data", rating: "wrong", thumbs: -1, overlap: 0, call_id: 11, skill_version: 3 }],
  replay_candidates: 4,
};
const replay = { asked: 1, answered: 1, rejected: 0, errors: 0, mean_overlap: 0.67, unsaved_text: true, seconds: 40,
  rows: [{ incident: "INC-0007", confirmed: "Backups filled /data", status: "trial", reason: "", said: "Backups filled the /data disk", overlap: 0.67, before: "CPU was high", before_overlap: 0 }] };

describe("ForgeAiTuning", () => {
  afterEach(() => (cleanup(), vi.unstubAllGlobals()));

  it("shows answers against confirmed causes and replays the unsaved text", async () => {
    const posts: unknown[] = [];
    vi.stubGlobal("fetch", vi.fn(async (url: string, init?: RequestInit) => {
      if (init?.method === "POST") posts.push(JSON.parse(String(init.body)));
      const body = url.endsWith("/tuning") ? report : url.endsWith("/replay") ? { job: 8 } : { status: "done", error: "", result: replay };
      return new Response(JSON.stringify(body), { status: 200, headers: { "Content-Type": "application/json" } });
    }));
    render(<ForgeAiTuning slug="linux-disk" instructions="New text that looks at backups first." changed canEdit />);
    expect(await screen.findByText("Backups filled /data")).toBeTruthy();
    expect(screen.getByText(/75% correct/)).toBeTruthy();
    expect(screen.getByText(/number that is not in the facts/)).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: /Replay the unsaved text on the last 4/ }));
    expect(await screen.findByText("Backups filled the /data disk")).toBeTruthy();
    expect(posts[0]).toEqual({ limit: 10, instructions: "New text that looks at backups first." });
  });
});
