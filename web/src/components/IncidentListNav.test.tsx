import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { MemoryRouter, Route, Routes, useParams } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { saveIncidentList } from "../lib/incidentList";
import { IncidentListNav } from "./IncidentListNav";

function Page() {
  const { number = "" } = useParams();
  return <><IncidentListNav number={number} /><span>at {number}</span></>;
}

describe("IncidentListNav", () => {
  let urls: string[];
  beforeEach(() => {
    urls = [];
    window.sessionStorage.clear();
    vi.stubGlobal("fetch", vi.fn(async (url: string) => {
      urls.push(url);
      const n = url.includes("INC-0002") ? { number: "INC-0002", tab: "active", position: 2, total: 4, prev: { number: "INC-0001", title: "a" }, next: { number: "INC-0003", title: "c" } }
        : { number: "INC-0003", tab: "active", position: 3, total: 4, prev: { number: "INC-0002", title: "b" }, next: null };
      return new Response(JSON.stringify(n), { status: 200, headers: { "Content-Type": "application/json" } });
    }));
  });
  afterEach(() => (cleanup(), vi.unstubAllGlobals()));

  const renderAt = (n: string) => render(
    <MemoryRouter initialEntries={[`/incidents/${n}`]}>
      <Routes>
        <Route path="/incidents/:number" element={<Page />} />
        <Route path="/incidents" element={<span>the list</span>} />
      </Routes>
    </MemoryRouter>,
  );

  it("steps through the remembered list with the buttons and the arrow keys", async () => {
    saveIncidentList({ tab: "active", severity: "CRITICAL", q: "", date_from: "", date_to: "", team: "", offset: 0 });
    renderAt("INC-0002");
    expect(await screen.findByText(/2 of 4 · Not acknowledged, filtered/)).toBeTruthy();
    expect(urls[0]).toContain("tab=active");
    expect(urls[0]).toContain("severity=CRITICAL");
    fireEvent.click(screen.getByRole("button", { name: /Next/ }));
    expect(await screen.findByText("at INC-0003")).toBeTruthy();
    expect(await screen.findByText(/3 of 4/)).toBeTruthy();
    expect((screen.getByRole("button", { name: /Next/ }) as HTMLButtonElement).disabled).toBe(true);
    fireEvent.keyDown(window, { key: "ArrowLeft" });
    expect(await screen.findByText("at INC-0002")).toBeTruthy();
  });

  it("goes back to the incident list", async () => {
    renderAt("INC-0002");
    fireEvent.click(await screen.findByRole("link", { name: /Back to incidents/ }));
    expect(await screen.findByText("the list")).toBeTruthy();
  });
});
