import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { Automation, Simulation } from "../lib/api";
import { AutomationPage } from "./Automation";

const data: Automation = {
  pipeline: { incidents_today: 12, muted_today: 3, routed_today: 9, notifications_today: 7 },
  rules: [{ id: 1, name: "high-cpu", description: "CPU above 80%", enabled: true, severity: "warning", condition: { alertname: "HighCPU", metric: "cpu_usage", operator: ">", value: 80 }, playbook: "cpu", policy: "Default warning", hits_30d: 4 }],
  playbooks: [{ id: 1, name: "cpu", description: "", steps: ["Check top processes"], rules: 1 }],
  policies: [{ id: 1, name: "Default warning", steps: [{ after_minutes: 0, target: "team", channel: "email", level: 1 }, { after_minutes: 15, target: "team-lead", channel: "email", level: 2 }], rules: 1 }],
  maintenance: [],
  can_edit: true,
};
const sim: Simulation = { outcome: "incident", trace: ["Rule “high-cpu” matches (warning)."], rule: "high-cpu", playbook: "cpu", policy: "Default warning", steps: data.policies[0].steps };

describe("Automation", () => {
  let bodies: string[];
  beforeEach(() => {
    bodies = [];
    vi.stubGlobal("fetch", vi.fn(async (url: string, init?: RequestInit) => {
      if (init?.body) bodies.push(init.body as string);
      return new Response(JSON.stringify(url.endsWith("/simulate") ? sim : data), { status: 200, headers: { "Content-Type": "application/json" } });
    }));
  });
  afterEach(() => (cleanup(), vi.unstubAllGlobals()));

  it("shows the pipeline, rules and escalation steps", async () => {
    render(<AutomationPage />);
    expect(await screen.findByText("12 incidents today")).toBeTruthy();
    expect(screen.getByText("HighCPU and cpu_usage > 80")).toBeTruthy();
    fireEvent.click(screen.getByRole("tab", { name: "Escalation" }));
    expect(screen.getByText("+15 min")).toBeTruthy();
  });

  it("runs the alarm simulator", async () => {
    render(<AutomationPage />);
    fireEvent.change(await screen.findByLabelText("Value"), { target: { value: "96" } });
    fireEvent.click(screen.getByRole("button", { name: "Run test" }));
    expect(await screen.findByText("Rule “high-cpu” matches (warning).")).toBeTruthy();
    await waitFor(() => expect(JSON.parse(bodies[0]).value).toBe("96"));
  });
});
