import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { afterEach, describe, expect, it, vi } from "vitest";
import { ShiftLine, ShiftPanel } from "./Handover";
import { draftText } from "./ShiftSummary";

const handover = { id: 3, created_by: "ana@dc.local", created_at: "2026-11-24T07:00:00Z", notes: "Watch db-01", open_incidents: [], taken_by: "", taken_at: null };

describe("Shift", () => {
  afterEach(() => (cleanup(), vi.unstubAllGlobals()));

  it("puts on call now, the handover and the ForgeAI draft in one card", async () => {
    vi.stubGlobal("fetch", vi.fn(async (url: string) => new Response(JSON.stringify(
      url === "/api/v1/handover" ? { handovers: [handover] } : { enabled: true }), { status: 200, headers: { "Content-Type": "application/json" } })));
    render(<ShiftPanel now={{ person: "Marko", phone: "+381 60 2" }} canWork me="marko@dc.local" />);
    expect(screen.getByText("Marko")).toBeTruthy();
    expect(await screen.findByText("Watch db-01")).toBeTruthy();
    expect(screen.getByText("not taken over")).toBeTruthy();
    expect(screen.getByRole("button", { name: "I take over" })).toBeTruthy();
    expect(await screen.findByRole("button", { name: "Draft with ForgeAI" })).toBeTruthy();
  });

  it("the NOC shows one line that links to the shift", () => {
    render(<MemoryRouter><ShiftLine oncall={{ name: "Marko", phone: "+381 60 2" }} handover={handover} /></MemoryRouter>);
    expect(screen.getByText("Marko")).toBeTruthy();
    expect(screen.getByRole("link", { name: "Shift and schedule →" }).getAttribute("href")).toBe("/incidents/on-call");
  });

  it("a ForgeAI draft becomes plain handover notes", () => {
    expect(draftText({ summary: "Quiet night.", still_open: ["INC-1 disk"], watch: [] })).toBe("Quiet night.\n\nStill open:\n- INC-1 disk");
  });

  it("the handover form opens with a draft in it", async () => {
    const { HandoverPart } = await import("./Handover");
    render(<HandoverPart handover={null} canWork me="a@dc.local" onChanged={() => undefined} draft="From ForgeAI" />);
    expect((screen.getByLabelText("Handover notes") as HTMLTextAreaElement).value).toBe("From ForgeAI");
    fireEvent.click(screen.getByRole("button", { name: "Cancel" }));
  });
});
