import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { loadIncidentList, saveIncidentList } from "../lib/incidentList";
import { IncidentsPage } from "./Incidents";

describe("Incidents page", () => {
  let urls: string[];
  beforeEach(() => {
    urls = [];
    window.sessionStorage.clear();
    vi.stubGlobal("fetch", vi.fn(async (url: string) => {
      urls.push(url);
      const body = url.includes("incidents-page") ? { tab: "resolved", counts: { active: 1, acknowledged: 0, resolved: 30, all: 31 }, total: 30, offset: 10, limit: 10, rows: [], teams: [] } : { items: [], views: [] };
      return new Response(JSON.stringify(body), { status: 200, headers: { "Content-Type": "application/json" } });
    }));
  });
  afterEach(() => (cleanup(), vi.unstubAllGlobals()));

  it("comes back to the list it was on and remembers a new choice", async () => {
    saveIncidentList({ tab: "resolved", severity: "HIGH", q: "", date_from: "", date_to: "", team: "", offset: 10 });
    render(<MemoryRouter><IncidentsPage /></MemoryRouter>);
    await vi.waitFor(() => expect(urls.some((u) => u.includes("tab=resolved") && u.includes("severity=HIGH") && u.includes("offset=10"))).toBe(true));
    fireEvent.click(screen.getByRole("tab", { name: /All/ }));
    expect(loadIncidentList()).toMatchObject({ tab: "all", severity: "HIGH", offset: 0 });
  });
});
