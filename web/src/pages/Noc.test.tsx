import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { afterEach, describe, expect, it } from "vitest";
import type { Noc } from "../lib/api";
import { NocContext, NocPage } from "./Noc";

const row = (number: string, severity: string, hostname: string, extra: Partial<Noc["incidents"]["active"][number]> = {}) => ({
  number, severity, status: "OPEN", title: `${hostname} problem`, detail: "", source: "prometheus",
  started_at: new Date(Date.now() - 6 * 60000).toISOString(), ack_at: null, ack_by: "", resolved_at: null,
  asset: { hostname, type: "Linux Server", ip: "10.0.0.1" }, level: "L1", messages: 2, forgerca: "Disk filling", forgeai: "ready-local" as const,
  ...extra,
});

const data: Noc = {
  generated_at: new Date().toISOString(),
  counts: { open: 3, critical: 1, high: 1, warning: 1, not_acked: 2, hosts_total: 12, hosts_down: 1, hosts_down_names: ["esx-prod-03"], mtta_today_min: 4, mttr_7d_min: 38 },
  incidents: {
    active: [row("INC-1", "CRITICAL", "esx-prod-03"), row("INC-2", "WARNING", "nas-bkp-01")],
    acknowledged: [row("INC-3", "HIGH", "db-ora-02", { ack_at: new Date().toISOString(), ack_by: "ana@dc.local" })],
    resolved_today: [],
  },
  health: [{ group: "Network", up: 9, total: 10 }],
  oncall: { name: "Marko J.", email: "marko@dc.local", phone: "" },
  ai: { forgerca: true, forgeai: "local" },
};

function renderPage() {
  return render(
    <MemoryRouter>
      <NocContext.Provider value={{ data, error: "" }}>
        <NocPage />
      </NocContext.Provider>
    </MemoryRouter>,
  );
}

describe("NOC overview", () => {
  afterEach(cleanup);

  it("shows the key numbers, the not-acknowledged list and the side cards", () => {
    renderPage();
    expect(screen.getByText("Open incidents")).toBeTruthy();
    expect(screen.getByText("1 / 12")).toBeTruthy();
    expect(screen.getByText("esx-prod-03 problem")).toBeTruthy();
    expect(screen.getByText("nas-bkp-01 problem")).toBeTruthy();
    expect(screen.queryByText("db-ora-02 problem")).toBeNull();
    expect(screen.getAllByText("Analysed").length).toBe(2);
    expect(screen.getByText("Marko J.")).toBeTruthy();
  });

  it("switches tabs and filters by severity and text", () => {
    renderPage();
    fireEvent.click(screen.getByRole("tab", { name: /Acknowledged/ }));
    expect(screen.getByText("db-ora-02 problem")).toBeTruthy();
    fireEvent.click(screen.getByRole("tab", { name: /Not acknowledged/ }));
    fireEvent.click(screen.getByRole("button", { name: "Critical" }));
    expect(screen.queryByText("nas-bkp-01 problem")).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: "All severities" }));
    fireEvent.change(screen.getByRole("searchbox", { name: "Filter incidents" }), { target: { value: "nas-bkp" } });
    expect(screen.getByText("nas-bkp-01 problem")).toBeTruthy();
    expect(screen.queryByText("esx-prod-03 problem")).toBeNull();
  });
});
