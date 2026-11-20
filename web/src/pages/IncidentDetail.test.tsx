import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { IncidentView } from "../lib/api";
import { IncidentDetailPage } from "./IncidentDetail";

const view: IncidentView = {
  number: "INC-0412", title: "esx-prod-03 unreachable", severity: "CRITICAL", status: "OPEN", source: "prometheus", summary: "",
  started_at: new Date(Date.now() - 6 * 60000).toISOString(), ack_at: null, ack_by: "", resolved_at: null, resolved_by: "",
  level: "L2", levels: [],
  asset: { asset_id: "esx-prod-03", hostname: "esx-prod-03", ip: "10.10.20.13", type: "ESXi host", environment: "prod", owner: "Compute", contact_name: "Marko", owner_email: "", owner_phone: "", notes: "", glpi_id: "1182", zabbix_hostid: "" },
  playrule: "Compute host down",
  playbook: { name: "HOST-UNREACHABLE", steps: ["Confirm host is down", "Check upstream switch port"], done: [0] },
  forgerca: { cause: "Uplink failure on sw-acc-07", summary: "", confidence: 86, facts: [{ id: "f1", text: "node_exporter up = 0" }], hypotheses: [{ id: "h1", summary: "Uplink failure", confidence: 0.86, rank: 1 }], actions: ["Check the SFP."], limitations: [], at: null },
  forgeai: { mode: "local", text: "The host went dark after its uplink dropped.", cause: "", action: "", model: "local", at: null },
  evidence: [], timeline: [], notifications: [], notes: [],
  review: { root_cause: "", rca_rating: "", ai_rating: "", reviewed_by: "", reviewed_at: null },
  can: { work: true, investigate: true, cloud: false },
  events: [{ at: "2026-11-20T06:00:00Z", kind: "alarm", who: "prometheus", text: "Alarm fired: esx-prod-03 unreachable" }, { at: "2026-11-20T06:00:05Z", kind: "notify", who: "email", text: "email to noc@dc.local · sent" }],
  who_to_call: [{ role: "On call now", name: "Ana P.", phone: "+381 60 111", email: "ana@dc.local" }],
  similar: [{ number: "INC-0301_01.11.2026_03:10", status: "RESOLVED", started_at: "2026-11-01T02:10:00Z", minutes: 42, root_cause: "SFP replaced", resolved_by: "marko" }],
};
const metrics = { collecting: true, collecting_line: "", error: "", window: { start: 1000, end: 4600, marker: 4600 - 360, label: "last 1 h" },
  tiles: [{ key: "cpu", name: "CPU", display: "93%", tone: "crit", threshold: 90, series: [10, 50, 93], times: [1000, 2800, 4600] }] };

describe("Incident detail", () => {
  let calls: { url: string; body?: string }[];
  beforeEach(() => {
    calls = [];
    vi.stubGlobal("fetch", vi.fn(async (url: string, init?: RequestInit) => {
      calls.push({ url, body: init?.body as string | undefined });
      const body = url.endsWith("/view") ? view : url.includes("/metrics") ? metrics : url.endsWith("/syslog") ? { lines: ["%LINK-3-UPDOWN: Interface Gi1/0/14, changed state to down"], note: "" } : {};
      return new Response(JSON.stringify(body), { status: 200, headers: { "Content-Type": "application/json" } });
    }));
  });
  afterEach(() => {
    cleanup();
    vi.unstubAllGlobals();
  });

  const renderPage = () =>
    render(
      <MemoryRouter initialEntries={["/incidents/INC-0412"]}>
        <Routes>
          <Route path="/incidents/:number" element={<IncidentDetailPage />} />
        </Routes>
      </MemoryRouter>,
    );

  it("shows ForgeRCA, ForgeAI, the asset and the playbook", async () => {
    renderPage();
    expect(await screen.findByText("esx-prod-03 unreachable")).toBeTruthy();
    expect(screen.getByText("Uplink failure on sw-acc-07")).toBeTruthy();
    expect(screen.getByText("node_exporter up = 0")).toBeTruthy();
    expect(screen.getByText("The host went dark after its uplink dropped.")).toBeTruthy();
    expect(screen.getByText("Playbook · HOST-UNREACHABLE")).toBeTruthy();
    expect(screen.getByText("1 / 2")).toBeTruthy();
  });

  it("acknowledges and ticks a checklist step through the API", async () => {
    renderPage();
    fireEvent.click(await screen.findByRole("button", { name: "Acknowledge" }));
    await waitFor(() => expect(calls.some((c) => c.url.endsWith("/status") && c.body === JSON.stringify({ status: "INVESTIGATING" }))).toBe(true));
    fireEvent.click(screen.getByRole("checkbox", { name: "Check upstream switch port" }));
    await waitFor(() => expect(calls.some((c) => c.url.endsWith("/checklist") && c.body === JSON.stringify({ step: 1, done: true }))).toBe(true));
  });

  it("saves the analyst review", async () => {
    renderPage();
    fireEvent.change(await screen.findByLabelText("What really caused it"), { target: { value: "Failing SFP" } });
    fireEvent.click(screen.getAllByRole("radio", { name: "Correct" })[0]);
    fireEvent.click(screen.getByRole("button", { name: "Save review" }));
    await waitFor(() =>
      expect(calls.some((c) => c.url.endsWith("/review") && c.body === JSON.stringify({ root_cause: "Failing SFP", rca_rating: "correct", ai_rating: "" }))).toBe(true),
    );
  });

  it("shows who to call, what happened before, the story, metrics and syslog", async () => {
    renderPage();
    expect(await screen.findByText("Ana P.")).toBeTruthy();
    expect(screen.getByRole("link", { name: "+381 60 111" }).getAttribute("href")).toBe("tel:+38160111");
    expect(screen.getByText("SFP replaced")).toBeTruthy();
    expect(screen.getByText("INC-0301")).toBeTruthy();
    expect(screen.getByText("email to noc@dc.local · sent")).toBeTruthy();
    expect(screen.getByText("Check the SFP.")).toBeTruthy();
    expect(await screen.findByRole("img", { name: "CPU over the incident, now 93%" })).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "Show syslog" }));
    expect(await screen.findByText(/Gi1\/0\/14, changed state to down/)).toBeTruthy();
    expect(calls.some((c) => c.url === "/api/v1/assets/esx-prod-03/metrics?incident=INC-0412")).toBe(true);
  });
});
