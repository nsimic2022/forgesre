import { cleanup, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { PostmortemPanel } from "./PostmortemPanel";

describe("Postmortem panel", () => {
  afterEach(() => (cleanup(), vi.unstubAllGlobals()));
  it("shows the draft, counts the gaps and offers a download", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => new Response(JSON.stringify({ markdown: "# Incident review: INC-1\n\n- [to fill]\n| [to fill] | [to fill] |\n" }), { status: 200, headers: { "Content-Type": "application/json" } })));
    render(<PostmortemPanel number="INC-1" onClose={() => undefined} />);
    expect(await screen.findByText("3 gaps to fill in the review")).toBeTruthy();
    expect(screen.getByRole("link", { name: "Download .md" }).getAttribute("href")).toBe("/api/v1/incidents/INC-1/postmortem?download=1");
  });
});
