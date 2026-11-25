import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { afterEach, describe, expect, it, vi } from "vitest";
import type { NocIncident } from "../lib/api";
import { IncidentTable } from "./IncidentTable";

const row = (number: string, forgeai: NocIncident["forgeai"], extra: Partial<NocIncident> = {}): NocIncident => ({
  number, severity: "CRITICAL", status: "OPEN", title: `${number} disk full`, detail: "", source: "zabbix", started_at: "2026-11-25T08:00:00Z",
  ack_at: null, ack_by: "", resolved_at: null, asset: { hostname: "db-01", type: "Linux Server", ip: "10.0.0.1" }, level: "L1", messages: 0,
  forgerca: "Disk filling", forgeai, flap_count: 0, sla: null, major: false, caused_by: "", ...extra,
} as NocIncident);

describe("ForgeAI column", () => {
  afterEach(() => (cleanup(), vi.unstubAllGlobals()));

  it("shows done, waiting with the queue place, failed and grouped, and opens the analysis under the row", async () => {
    const calls: string[] = [];
    vi.stubGlobal("fetch", vi.fn(async (url: string, init?: RequestInit) => {
      calls.push(`${init?.method ?? "GET"} ${url}`);
      const body = url.endsWith("/forgeai") ? { queued: true }
        : { forgeai: { text: "Backups filled /var.", cause: "The nightly backup filled /var", action: "Check df -h on /var", model: "qwen3-8b", at: "2026-11-25T08:02:00Z" }, rca: { cause: "Disk filling", actions: [] } };
      return new Response(JSON.stringify(body), { status: 200, headers: { "Content-Type": "application/json" } });
    }));
    render(<MemoryRouter><IncidentTable loaded canWork rows={[row("INC-1", "ready-local"), row("INC-2", "queued", { forgeai_queue: 2 }), row("INC-3", "failed"),
      row("INC-4", "grouped", { forgeai_ref: "INC-1", forgeai_ref_number: "INC-1" })]} /></MemoryRouter>);
    expect(screen.getByText("Analysed")).toBeTruthy();
    expect(screen.getByText("#2 in queue")).toBeTruthy();
    expect(screen.getByText("Failed")).toBeTruthy();
    expect(screen.getByText("with INC-1")).toBeTruthy();
    const cell = screen.getByRole("button", { name: /ForgeAI Analysed for INC-1/ });
    fireEvent.click(cell);
    expect(cell.getAttribute("aria-expanded")).toBe("true");
    expect(await screen.findByText("The nightly backup filled /var")).toBeTruthy();
    fireEvent.click(cell);
    expect(screen.queryByText("The nightly backup filled /var")).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: /ForgeAI Grouped for INC-4/ }));
    expect(await screen.findByText(/its analysis covers this one/)).toBeTruthy();
    expect(calls).toContain("GET /api/v1/incidents/INC-1");
    fireEvent.click(screen.getByRole("button", { name: "Analyse this one on its own" }));
    expect(await screen.findByText("Queued.")).toBeTruthy();
    expect(calls).toContain("POST /api/v1/incidents/INC-4/forgeai");
  });
});
