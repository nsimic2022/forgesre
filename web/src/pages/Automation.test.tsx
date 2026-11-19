import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { Automation, Simulation } from "../lib/api";
import { AutomationPage } from "./Automation";

const data: Automation = {
  filters: [{ id: 4, name: "Backup window", enabled: true, alertnames: ["DiskUsage"], scope: "type", scope_value: "Oracle DB", days: [], start_minute: 60, end_minute: 180, max_value: 97, action: "quiet", delay_minutes: 5, expires_at: null, owner: "dba", note: "nightly backup", hits: 41, last_hit_at: null, created_by: "x" }],
  pipeline: { incidents_today: 12, filtered_today: 2, muted_today: 3, routed_today: 9, notifications_today: 7 },
  rules: [{ id: 1, name: "high-cpu", description: "CPU above 80%", enabled: true, severity: "warning", condition: { alertname: "HighCPU", metric: "cpu_usage", operator: ">", value: 80 }, playbook: "cpu", playbook_id: 1, policy: "Default warning", policy_id: 1, hits_30d: 4 }],
  playbooks: [{ id: 1, name: "cpu", description: "", steps: ["Check top processes"], rules: 1 }],
  policies: [{ id: 1, slug: "default-warning", name: "Default warning", levels: [{ level: 1, label: "L1 NOC", on: true, minutes: 0, target: "team" }, { level: 2, label: "L2 Shift", on: true, minutes: 15, target: "team-lead" }, { level: 3, label: "L3 Engineer", on: false, minutes: 30, target: "engineer" }, { level: 4, label: "L4 Manager", on: false, minutes: 60, target: "manager" }], steps: [{ after_minutes: 0, target: "team", channel: "email", level: 1 }, { after_minutes: 15, target: "team-lead", channel: "email", level: 2 }], rules: 1 }],
  maintenance: [{ asset: "db-ora-02", asset_id: "db-ora-02", summary: "Asset maintenance window", starts_at: null, ends_at: null, active: true }],
  can_edit: true,
  can_edit_maintenance: true,
};
const sim: Simulation = { outcome: "incident", trace: ["Rule “high-cpu” matches (warning)."], rule: "high-cpu", playbook: "cpu", policy: "Default warning", steps: data.policies[0].steps };

describe("Automation", () => {
  let bodies: string[];
  let calls: { url: string; method: string; body: unknown }[];
  beforeEach(() => {
    bodies = [];
    calls = [];
    vi.stubGlobal("fetch", vi.fn(async (url: string, init?: RequestInit) => {
      if (init?.body) bodies.push(init.body as string);
      calls.push({ url, method: init?.method ?? "GET", body: init?.body ? JSON.parse(String(init.body)) : null });
      return new Response(JSON.stringify(url.endsWith("/simulate") ? sim : data), { status: 200, headers: { "Content-Type": "application/json" } });
    }));
  });
  afterEach(() => (cleanup(), vi.unstubAllGlobals()));

  it("lists noise filters and saves a new one", async () => {
    render(<AutomationPage />);
    expect(await screen.findByText("Backup window")).toBeTruthy();
    expect(screen.getByText("every day, 01:00–03:00")).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "New filter" }));
    fireEvent.change(screen.getByLabelText("Name"), { target: { value: "Short spikes" } });
    fireEvent.click(screen.getByRole("radio", { name: /Wait before opening/ }));
    fireEvent.click(screen.getByRole("button", { name: "Save filter" }));
    await waitFor(() => {
      const sent = bodies.map((b) => JSON.parse(b)).find((b) => b.name === "Short spikes");
      expect(sent).toMatchObject({ action: "delay", delay_minutes: 5, scope: "all" });
    });
  });

  it("shows the pipeline, rules and escalation steps", async () => {
    render(<AutomationPage />);
    expect(await screen.findByText("12 incidents today")).toBeTruthy();
    fireEvent.click(screen.getByRole("tab", { name: "Routing rules" }));
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

  it("creates a rule, turns one off and removes it after asking", async () => {
    render(<AutomationPage />);
    await screen.findByText("12 incidents today");
    fireEvent.click(screen.getByRole("tab", { name: "Routing rules" }));
    fireEvent.click(screen.getByRole("button", { name: "New rule" }));
    fireEvent.change(screen.getByLabelText("Name"), { target: { value: "disk-db" } });
    fireEvent.change(screen.getByLabelText("Alert name (empty = rule name)"), { target: { value: "NodeFilesystemUsageHigh" } });
    fireEvent.change(screen.getByLabelText("Threshold"), { target: { value: "90" } });
    fireEvent.change(screen.getByLabelText("Playbook"), { target: { value: "1" } });
    fireEvent.click(screen.getByRole("button", { name: "Save rule" }));
    await waitFor(() => expect(calls.some((c) => c.method === "POST" && c.url === "/api/v1/automation/rules")).toBe(true));
    const made = calls.find((c) => c.method === "POST" && c.url === "/api/v1/automation/rules")!.body;
    expect(made).toMatchObject({ name: "disk-db", alertname: "NodeFilesystemUsageHigh", value: 90, playbook_id: 1, escalation_policy_id: null });

    fireEvent.click(await screen.findByRole("button", { name: "Turn off rule high-cpu" }));
    await waitFor(() => expect(calls.some((c) => c.method === "PUT" && c.url === "/api/v1/automation/rules/1")).toBe(true));
    expect(calls.find((c) => c.method === "PUT")!.body).toMatchObject({ enabled: false, alertname: "HighCPU", metric: "cpu_usage", value: 80 });

    fireEvent.click(screen.getByRole("button", { name: "Remove" }));
    expect(calls.some((c) => c.method === "DELETE")).toBe(false);
    fireEvent.click(screen.getByRole("button", { name: "Confirm remove high-cpu" }));
    await waitFor(() => expect(calls.some((c) => c.method === "DELETE" && c.url === "/api/v1/automation/rules/1")).toBe(true));
  });

  it("edits and copies a playbook", async () => {
    render(<AutomationPage />);
    await screen.findByText("12 incidents today");
    fireEvent.click(screen.getByRole("tab", { name: "Playbooks" }));
    fireEvent.click(screen.getByRole("button", { name: "Copy playbook cpu" }));
    expect((screen.getByLabelText("Name") as HTMLInputElement).value).toBe("cpu (copy)");
    fireEvent.change(screen.getByLabelText("Checklist steps, one per line"), { target: { value: "Check top processes\n\nCall the owner" } });
    fireEvent.click(screen.getByRole("button", { name: "Save playbook" }));
    await waitFor(() => expect(calls.some((c) => c.method === "POST" && c.url === "/api/v1/automation/playbooks")).toBe(true));
    expect(calls.find((c) => c.url === "/api/v1/automation/playbooks")!.body).toMatchObject({ name: "cpu (copy)", steps: ["Check top processes", "Call the owner"] });
  });

  it("edits escalation levels and does not offer to remove the default policy", async () => {
    render(<AutomationPage />);
    await screen.findByText("12 incidents today");
    fireEvent.click(screen.getByRole("tab", { name: "Escalation" }));
    expect(screen.queryByRole("button", { name: "Remove" })).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: "Edit policy Default warning" }));
    expect((screen.getByLabelText("L3 Engineer minutes") as HTMLInputElement).disabled).toBe(true);
    fireEvent.click(screen.getByLabelText("L3 Engineer on"));
    fireEvent.change(screen.getByLabelText("L3 Engineer recipient"), { target: { value: "dba@dc.local" } });
    fireEvent.click(screen.getByRole("button", { name: "Save policy" }));
    await waitFor(() => expect(calls.some((c) => c.method === "PUT" && c.url === "/api/v1/automation/policies/1")).toBe(true));
    const levels = (calls.find((c) => c.url === "/api/v1/automation/policies/1")!.body as { levels: unknown[] }).levels;
    expect(levels[2]).toEqual({ level: 3, on: true, minutes: 30, target: "dba@dc.local" });
    expect(levels[3]).toMatchObject({ level: 4, on: false });
  });

  it("starts maintenance for several assets and ends one now", async () => {
    render(<AutomationPage />);
    await screen.findByText("12 incidents today");
    fireEvent.click(screen.getByRole("tab", { name: "Maintenance windows" }));
    fireEvent.click(screen.getByRole("button", { name: "New window" }));
    fireEvent.change(screen.getByLabelText(/^Assets/), { target: { value: "db-ora-02, db-ora-03" } });
    fireEvent.change(screen.getByLabelText("From"), { target: { value: "2026-11-20T22:00" } });
    fireEvent.change(screen.getByLabelText("Until"), { target: { value: "2026-11-21T02:00" } });
    fireEvent.click(screen.getByRole("button", { name: "Start maintenance" }));
    await waitFor(() => expect(calls.some((c) => c.method === "POST" && c.url === "/api/v1/maintenance")).toBe(true));
    expect(calls.find((c) => c.url === "/api/v1/maintenance")!.body).toEqual({ assets: ["db-ora-02", "db-ora-03"], starts_at: "2026-11-20T22:00", ends_at: "2026-11-21T02:00" });
    fireEvent.click(await screen.findByRole("button", { name: "End maintenance on db-ora-02" }));
    await waitFor(() => expect(calls.some((c) => c.method === "DELETE" && c.url === "/api/v1/maintenance/db-ora-02")).toBe(true));
  });
});
