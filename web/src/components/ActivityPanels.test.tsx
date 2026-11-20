import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { afterEach, describe, expect, it, vi } from "vitest";
import { AuditPanel, JournalPanel, OutboxCard } from "./ActivityPanels";

const json = (body: unknown, status = 200) => new Response(JSON.stringify(body), { status, headers: { "Content-Type": "application/json" } });

describe("Activity panels", () => {
  afterEach(() => (cleanup(), vi.unstubAllGlobals()));

  it("lists the outbox, filters undelivered messages and hides itself without rights", async () => {
    const urls: string[] = [];
    vi.stubGlobal("fetch", vi.fn(async (url: string) => (urls.push(url), json({ total: 1, rows: [{ id: 1, at: null, channel: "email", target: "dba@dc.local", subject: "Alarm db-01", status: "failed", error: "SMTP 550", step: "immediate", incident: "INC-7_01.11.2026_01:00" }] }))));
    render(<MemoryRouter><OutboxCard /></MemoryRouter>);
    expect(await screen.findByText("SMTP 550")).toBeTruthy();
    expect(screen.getByText("INC-7")).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "Not delivered" }));
    await waitFor(() => expect(urls.some((u) => u.includes("status=failed"))).toBe(true));
    cleanup();
    vi.stubGlobal("fetch", vi.fn(async () => json({ detail: "forbidden" }, 403)));
    const { container } = render(<MemoryRouter><OutboxCard /></MemoryRouter>);
    await waitFor(() => expect(container.textContent).toBe(""));
  });

  it("shows journal errors and the audit log", async () => {
    vi.stubGlobal("fetch", vi.fn(async (url: string) => url.startsWith("/api/v1/journal")
      ? json({ modules: [{ module: "zabbix", count: 3 }], entries: [{ id: 1, at: null, module: "zabbix", action: "sync", status: "error", summary: "Zabbix sync failed", detail: "HTTP 401" }] })
      : json({ total: 1, rows: [{ id: 1, at: null, actor: "ana@dc.local", action: "rule.update", object: "rule:4", ip: "", data: { enabled: false } }] })));
    render(<><JournalPanel /><AuditPanel /></>);
    expect(await screen.findByText("Zabbix sync failed")).toBeTruthy();
    expect(screen.getByText("HTTP 401")).toBeTruthy();
    expect(await screen.findByText("rule.update")).toBeTruthy();
  });
});
