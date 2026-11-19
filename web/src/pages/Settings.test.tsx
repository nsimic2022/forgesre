import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { SettingsData } from "../lib/api";
import { SettingsPage } from "./Settings";

const data: SettingsData = {
  groups: { general: "General", ai: "AI · ForgeRCA / ForgeAI" },
  settings: [
    { key: "system.log_level", group: "general", label: "Log level", kind: "enum", store: "yaml", editable: true, help: "", test: "", choices: ["debug", "info"], value: "info", effective: null },
    { key: "ai.llm.timeout_seconds", group: "ai", label: "Model timeout (s)", kind: "int", store: "yaml", editable: true, help: "", test: "llm", value: 120, effective: 120 },
    { key: "ai.enabled", group: "ai", label: "ForgeAI enabled", kind: "bool", store: "yaml", editable: true, help: "", test: "llm", value: true, effective: true },
    { key: "SMTP_PASSWORD", group: "ai", label: "SMTP password", kind: "str", store: "secret", editable: false, help: "", test: "", set: false },
  ],
};

function respond(body: unknown, status = 200) {
  return new Response(JSON.stringify(body), { status, headers: { "Content-Type": "application/json" } });
}

describe("Settings", () => {
  let calls: { url: string; method?: string; body?: string }[];
  beforeEach(() => {
    calls = [];
    vi.stubGlobal("fetch", vi.fn(async (url: string, init?: RequestInit) => {
      calls.push({ url, method: init?.method, body: init?.body as string | undefined });
      if (url.startsWith("/api/v1/settings/test/")) return respond({ component: "llm", status: "error", why: "connection refused" });
      if (init?.method === "PUT") return respond({ ...data.settings[1], value: 300, effective: 300 });
      if (url === "/api/v1/system/doctor") return respond({ overall: "HEALTHY", components: {}, failed: [] });
      return respond(data);
    }));
  });
  afterEach(() => (cleanup(), vi.unstubAllGlobals()));

  it("edits a value, shows secret status and runs a connection test", async () => {
    render(<SettingsPage />);
    fireEvent.click(await screen.findByRole("button", { name: "AI · ForgeRCA / ForgeAI" }));
    fireEvent.change(screen.getByLabelText(/Model timeout/), { target: { value: "300" } });
    fireEvent.click(screen.getAllByRole("button", { name: "Save" })[0]);
    await waitFor(() => expect(calls.some((c) => c.method === "PUT" && c.url === "/api/v1/settings/ai.llm.timeout_seconds" && c.body === JSON.stringify({ value: "300" }))).toBe(true));
    expect(screen.getByText("Not set")).toBeTruthy();
    expect(screen.getByText("./forgesre settings set-secret SMTP_PASSWORD")).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "Test llm" }));
    expect(await screen.findByText("connection refused")).toBeTruthy();
  });

  it("tells non-admins settings are for Admins", async () => {
    vi.stubGlobal("fetch", vi.fn(async (url: string) => (url === "/api/v1/settings" ? respond({ detail: "forbidden" }, 403) : respond({ overall: "HEALTHY", components: {}, failed: [] }))));
    render(<SettingsPage />);
    expect(await screen.findByText(/Settings are for Admins/)).toBeTruthy();
  });
});
