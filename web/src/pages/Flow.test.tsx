import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { FlowPage } from "./Flow";

const now = new Date().toISOString();
const flow = {
  name: "zabbix", label: "Zabbix", about: "How alarms come in from Zabbix.", configured: true,
  status: "done", job: 3, watch: true,
  ai: { status: "done", at: now, question: "These blocks just broke: Zabbix sent", answer: "Open port 8080 for the Zabbix server.", model: "qwen3" },
  result: { checked_at: now, lanes: [
    { id: "in", title: "Zabbix → ForgeSRE (alarms, webhook)", steps: [
      { id: "media", label: "Media type", sub: "ForgeSRE", status: "ok", detail: "ForgeSRE: enabled.", fix: "", log: { direction: "out", kinds: ["mediatype.get"] } },
      { id: "sent", label: "Zabbix sent", sub: "2 recent", status: "bad", detail: "Last attempt: failed — Couldn't connect to server.", fix: "sudo ufw allow from <ZABBIX-SERVER-IP> to any port 8080 proto tcp",
        rows: [{ at: now, status: "failed", error: "Couldn't connect to server", retries: 3 }], log: { direction: "in", kinds: ["webhook"] } },
      { id: "network", label: "Network", sub: "→ :8080", status: "unknown", detail: "Nothing has arrived from Zabbix yet.", fix: "", log: { direction: "in", kinds: ["webhook"] } },
    ] },
    { id: "out", title: "ForgeSRE → Zabbix (API, read-only)", steps: [
      { id: "dns", label: "DNS", sub: "zbx", status: "ok", detail: "zbx → 10.0.0.9.", fix: "", ms: 2 },
      { id: "tls", label: "TLS", sub: "certificate", status: "warn", detail: "Trusted; expires in 12 day(s).", fix: "Renew it." },
    ] },
  ] },
};

describe("Flow page", () => {
  let urls: string[];
  beforeEach(() => {
    urls = [];
    vi.stubGlobal("fetch", vi.fn(async (url: string, init?: RequestInit) => {
      urls.push(`${init?.method ?? "GET"} ${url}`);
      let body: unknown = {};
      if (url.includes("/integrations/zabbix/flow")) body = init?.method === "POST" ? { job: 4 } : flow;
      else if (url.includes("/exchanges")) body = url.includes("after=")
        ? { total: 1, last_id: 9, rows: [{ id: 9, at: now, direction: "in", kind: "webhook", endpoint: "", peer: "10.0.0.9", status: "refused", http_status: 401, ms: 1, summary: "Wrong or missing token" }] }
        : { total: 1, last_id: 8, rows: [{ id: 8, at: now, direction: "in", kind: "webhook", endpoint: "", peer: "10.0.0.9", status: "ok", http_status: 200, ms: 40, summary: "1 event(s): High CPU" }] };
      else if (url.includes("/zabbix/logs")) body = { lines: ["core | zabbix webhook from 10.0.0.9"], error: "" };
      else if (url.includes("/forgeai/chats")) body = { id: 5, title: "Zabbix flow", incident: null, incident_label: "", topic: "flow:zabbix", topic_label: "Zabbix flow", updated_at: now, messages: [], skills: [] };
      return new Response(JSON.stringify(body), { status: 200, headers: { "Content-Type": "application/json" } });
    }));
  });
  afterEach(() => (cleanup(), vi.unstubAllGlobals()));

  const renderPage = () => render(<MemoryRouter initialEntries={["/settings/integrations/zabbix"]}><Routes><Route path="/settings/integrations/:name" element={<FlowPage />} /></Routes></MemoryRouter>);

  it("shows both lanes, opens the first broken block with its fix, log and Zabbix's attempts", async () => {
    renderPage();
    expect(await screen.findByRole("list", { name: "Zabbix → ForgeSRE (alarms, webhook)" })).toBeTruthy();
    const broken = screen.getByRole("button", { name: "Zabbix sent: Broken" });
    expect(broken.getAttribute("aria-pressed")).toBe("true");
    expect(screen.getByText("sudo ufw allow from <ZABBIX-SERVER-IP> to any port 8080 proto tcp")).toBeTruthy();
    expect(screen.getByRole("table", { name: "Details of this step" }).textContent).toContain("Couldn't connect to server");
    expect((await screen.findByRole("table", { name: "Exchange log" })).textContent).toContain("1 event(s): High CPU");
    expect(urls.some((u) => u.includes("exchanges?direction=in&kinds=webhook"))).toBe(true);
    expect(screen.getByText("Open port 8080 for the Zabbix server.")).toBeTruthy();
    expect(urls.some((u) => u.startsWith("POST") && u.includes("/zabbix/flow"))).toBe(false); // the result is fresh
  });

  it("switches the panel when another block is clicked, shows Core log lines and waits for a test", async () => {
    renderPage();
    fireEvent.click(await screen.findByRole("button", { name: "Network: Not checked" }));
    expect(screen.getByText("Nothing has arrived from Zabbix yet.")).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "Show" }));
    expect((await screen.findByLabelText("Log lines about Zabbix")).textContent).toContain("zabbix webhook");
    fireEvent.click(screen.getByRole("button", { name: "Wait for a test" }));
    expect(await screen.findByText("Waiting for a call…")).toBeTruthy();
    expect(await screen.findByText(/Wrong or missing token/, {}, { timeout: 5000 })).toBeTruthy();
  });

  it("checks again on request", async () => {
    renderPage();
    fireEvent.click(await screen.findByRole("button", { name: "Check again" }));
    await vi.waitFor(() => expect(urls.some((u) => u.startsWith("POST") && u.includes("/zabbix/flow"))).toBe(true));
  });
});
