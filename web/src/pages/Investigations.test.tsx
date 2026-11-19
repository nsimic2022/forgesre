import { cleanup, render, screen } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { InvestigationsPage as Page } from "../lib/api";
import { InvestigationsPage } from "./Investigations";

const page: Page = {
  days: 90, total: 1, offset: 0, limit: 50, reviewed: 1, resolved: 4,
  rows: [{ number: "INC-0388", title: "Interface down", severity: "HIGH", started_at: null, minutes: 38, host: "sw-acc-07", forgerca: "Uplink failure", forgeai: true, root_cause: "Failing SFP, replaced", rca_rating: "correct", ai_rating: "partly", reviewed_by: "ana", reviewed_at: null }],
  accuracy: { forgerca: { rated: 1, correct: 1, partly: 0, wrong: 0, score: 100 }, forgeai: { rated: 1, correct: 0, partly: 1, wrong: 0, score: 50 } },
  recurring: [{ cause: "Failing SFP, replaced", count: 3, hosts: ["sw-acc-07"] }],
};

describe("Investigations", () => {
  beforeEach(() => vi.stubGlobal("fetch", vi.fn(async () => new Response(JSON.stringify(page), { status: 200, headers: { "Content-Type": "application/json" } }))));
  afterEach(() => (cleanup(), vi.unstubAllGlobals()));

  it("shows history with ratings, accuracy and recurring causes", async () => {
    render(<MemoryRouter><InvestigationsPage /></MemoryRouter>);
    expect(await screen.findByText("INC-0388")).toBeTruthy();
    expect(screen.getAllByText("Failing SFP, replaced").length).toBe(2);
    expect(screen.getByText("100%")).toBeTruthy();
    expect(screen.getByText("50%")).toBeTruthy();
    expect(screen.getByText("3×")).toBeTruthy();
    expect(screen.getByText("Partly")).toBeTruthy();
  });
});
