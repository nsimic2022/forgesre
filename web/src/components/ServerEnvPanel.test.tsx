import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { ServerEnvPanel } from "./ServerEnvPanel";

const env = (installed: boolean) => ({
  groups: { ai: "ForgeAI model", general: "General" },
  rows: [
    { key: "FORGESRE_LLM_THREADS", group: "ai", label: "Model CPU threads", kind: "int", value: "8", in_file: true, default: "8", editable: true, restart: ["llm", "llm-gpu"], minimum: 1, maximum: 128, choices: [], help: "" },
    { key: "POSTGRES_PASSWORD", group: "general", label: "Postgres password", kind: "secret", set: true, in_file: true, editable: false, restart: [], help: "" },
  ],
  unlisted: [], secrets: [{ key: "GLPI_APP_TOKEN", group: "integrations", label: "GLPI app token", set: false, cli: true }],
  helper: { installed, waiting: 0, stuck: [] }, requests: [], answer_lengths: [{ task: "chat", label: "Chat", base: 800, now: 800 }],
});

describe("Settings › Server", () => {
  afterEach(() => (cleanup(), vi.unstubAllGlobals(), vi.useRealTimers()));

  it("shows values read only while the apply helper is off; passwords only as set", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => new Response(JSON.stringify(env(false)), { status: 200, headers: { "Content-Type": "application/json" } })));
    render(<ServerEnvPanel />);
    expect(await screen.findByText(/The apply helper is off/)).toBeTruthy();
    expect(screen.queryByLabelText("FORGESRE_LLM_THREADS")).toBeNull();
    expect(screen.getAllByText("set").length).toBeGreaterThan(0);
    expect(screen.getByText("./forgesre settings set-secret GLPI_APP_TOKEN")).toBeTruthy();
  });

  it("sends a change to the helper and shows the result", async () => {
    const calls: string[] = [];
    vi.stubGlobal("fetch", vi.fn(async (url: string, init?: RequestInit) => {
      calls.push(`${init?.method ?? "GET"} ${url} ${init?.body ?? ""}`);
      const body = url.includes("/requests/") ? { id: "20261125100000-abcdef", status: "ok", message: "1 value(s) written to .env; restarted llm" }
        : init?.method === "POST" ? { id: "20261125100000-abcdef", changes: { FORGESRE_LLM_THREADS: "12" }, restarts: ["llm", "llm-gpu"] } : env(true);
      return new Response(JSON.stringify(body), { status: 200, headers: { "Content-Type": "application/json" } });
    }));
    render(<ServerEnvPanel />);
    fireEvent.change(await screen.findByLabelText("FORGESRE_LLM_THREADS"), { target: { value: "12" } });
    expect(screen.getByText(/Will restart: llm, llm-gpu/)).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "Apply 1 change" }));
    await waitFor(() => expect(calls.some((c) => c.startsWith('POST /api/v1/server/env {"changes":{"FORGESRE_LLM_THREADS":"12"}}'))).toBe(true));
    expect(await screen.findByText(/restarted llm/, {}, { timeout: 4000 })).toBeTruthy();
  });
});
