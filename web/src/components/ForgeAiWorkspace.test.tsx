import { cleanup, render, screen } from "@testing-library/react";
import { MemoryRouter, Route, Routes, useLocation } from "react-router-dom";
import { afterEach, describe, expect, it, vi } from "vitest";
import { ForgeAiPage } from "../pages/ForgeAi";
import { ForgeAiWorkspace } from "./ForgeAiWorkspace";

const auto = { id: 1, at: null, task: "incident", skill: "linux-disk", skill_version: 1, model: "qwen", status: "used", reason: "", duration_ms: 41000, rating: 0, actor: "system",
  answer: { cause: "Archive logs filled /u02" }, system_prompt: "", user_prompt: "{}", raw_response: "{}" };

function stub(posts: { url: string; body: unknown }[], role = "analyst") {
  vi.stubGlobal("fetch", vi.fn(async (url: string, init?: RequestInit) => {
    if (init?.method === "POST") posts.push({ url, body: JSON.parse(String(init.body)) });
    const body =
      url === "/api/v1/me" ? { email: "a@dc.local", role, name: "A" }
      : url === "/api/v1/forgeai/chats" && init?.method === "POST" ? { id: 4 }
      : url === "/api/v1/forgeai/chats" ? { chats: [{ id: 4, title: "INC-0001 Disk full", incident: "INC-0001", incident_label: "INC-0001 Disk full", updated_at: null }], pending: 0, local_on: true, can_cloud: false }
      : url === "/api/v1/forgeai/chats/4" ? { id: 4, title: "INC-0001 Disk full", incident: "INC-0001", incident_label: "INC-0001 Disk full", updated_at: null, messages: [], skills: [] }
      : url.startsWith("/api/v1/incidents/INC-0001/forgeai") ? { calls: [auto] }
      : {};
    return new Response(JSON.stringify(body), { status: 200, headers: { "Content-Type": "application/json" } });
  }));
}

function Where() {
  const loc = useLocation();
  return <span data-testid="where">{loc.pathname + loc.search + loc.hash}</span>;
}

describe("ForgeAI on Investigations", () => {
  afterEach(() => (cleanup(), vi.unstubAllGlobals()));

  it("Ask on a row opens the incident chat with what ForgeAI found on its own", async () => {
    const posts: { url: string; body: unknown }[] = [];
    stub(posts);
    render(<MemoryRouter initialEntries={["/investigations"]}><ForgeAiWorkspace about="INC-0001" aboutKey={1} /><Where /></MemoryRouter>);
    expect(await screen.findByText("Archive logs filled /u02")).toBeTruthy();
    expect(posts[0]).toEqual({ url: "/api/v1/forgeai/chats", body: { incident: "INC-0001", reuse: true } });
    expect(screen.getByTestId("where").textContent).toContain("chat=4");
  });

  it("the old /forgeai link lands on Investigations › Ask ForgeAI", () => {
    render(
      <MemoryRouter initialEntries={["/forgeai?chat=4"]}>
        <Routes>
          <Route path="/forgeai" element={<ForgeAiPage />} />
          <Route path="/investigations" element={<Where />} />
        </Routes>
      </MemoryRouter>,
    );
    expect(screen.getByTestId("where").textContent).toBe("/investigations?chat=4#ask-forgeai");
  });
});
