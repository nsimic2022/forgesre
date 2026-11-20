import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { afterEach, describe, expect, it, vi } from "vitest";
import { snmpPayload } from "../components/SnmpFields";
import { AddDevicesPage } from "./AddDevices";

const found = {
  ip: "10.0.0.7", alive: true, open_ports: [22, 161], exporter: "network", detect_message: "",
  snmp: { sys_descr: "Cisco IOS Catalyst 9300 Switch", sys_name: "sw-core-01", sys_location: "DC1" }, snmp_note: "",
  watch: "snmp_exporter walks it over SNMP",
  suggested: { hostname: "sw-core-01", type: "Switch", monitoring_profile: "network-switch", scrape_address: "", vendor: "Cisco", location: "DC1" },
};
const checklist = { asset_id: "sw-core-01", overall: "PASS", reason: "", hint: "", class: "network", steps: [
  { key: "icmp", title: "The address answers", state: "ok", detail: "1 ms", fix: "" },
  { key: "target", title: "Prometheus scrapes it (up = 1)", state: "ok", detail: "", fix: "" },
] };

describe("Add devices", () => {
  afterEach(() => (cleanup(), vi.unstubAllGlobals()));

  it("identifies with the custom community, adds the switch and shows the monitoring check", async () => {
    const posts: { url: string; body: any }[] = [];
    vi.stubGlobal("fetch", vi.fn(async (url: string, init?: RequestInit) => {
      if (init?.method === "POST") posts.push({ url, body: JSON.parse(String(init.body)) });
      const body =
        url === "/api/v1/inventory-form" ? { types: [{ group: "Network", names: ["Switch", "Router"] }], extra_fields: [], environments: ["Production"], owners: [], sites: [], can_edit: true }
        : url === "/api/v1/onboard/identify" ? { job: 1 }
        : url === "/api/v1/onboard/jobs/1" ? { id: 1, kind: "onboard_identify", status: "done", error: "", result: found }
        : url === "/api/v1/assets" ? { asset_id: "sw-core-01" }
        : url.endsWith("/verify-job") ? { job: 2, snmp_apply: true }
        : url === "/api/v1/onboard/jobs/2" ? { id: 2, kind: "asset_verify", status: "done", error: "", result: checklist }
        : {};
      return new Response(JSON.stringify(body), { status: 200, headers: { "Content-Type": "application/json" } });
    }));
    render(<MemoryRouter><AddDevicesPage /></MemoryRouter>);
    fireEvent.change(screen.getByLabelText("IP address"), { target: { value: "10.0.0.7" } });
    fireEvent.change(screen.getByLabelText("Community"), { target: { value: "custom" } });
    fireEvent.change(screen.getByLabelText("Community string"), { target: { value: "noc-ro" } });
    fireEvent.click(screen.getByRole("button", { name: "Identify" }));
    expect(await screen.findByText("Cisco IOS Catalyst 9300 Switch")).toBeTruthy();
    expect(posts[0].body.snmp).toEqual({ snmp_version: "v2c", snmp_community_mode: "custom", snmp_community: "noc-ro" });
    expect((screen.getByLabelText("Hostname") as HTMLInputElement).value).toBe("sw-core-01");
    fireEvent.click(screen.getByRole("button", { name: "Add and start monitoring" }));
    expect(await screen.findByText("Monitored")).toBeTruthy();
    const created = posts.find((p) => p.url === "/api/v1/assets")!.body;
    expect(created).toMatchObject({ hostname: "sw-core-01", type: "Switch", ip: "10.0.0.7", extras: { snmp_community: "noc-ro", site: "DC1" } });
    await waitFor(() => expect(screen.getByText("Prometheus scrapes it (up = 1)")).toBeTruthy());
  });

  it("sends only the SNMP keys the chosen version uses", () => {
    expect(snmpPayload({ snmp_version: "v3", snmp_v3_user: "noc", snmp_v3_level: "authNoPriv", snmp_v3_auth_pass: "12345678", snmp_community: "x" }))
      .toEqual({ snmp_version: "v3", snmp_v3_user: "noc", snmp_v3_level: "authNoPriv", snmp_v3_auth_proto: "SHA", snmp_v3_auth_pass: "12345678" });
    expect(snmpPayload({ snmp_version: "v2c", snmp_community_mode: "default", snmp_community: "x" })).toEqual({ snmp_version: "v2c", snmp_community_mode: "default" });
  });

  it("reads LibreNMS instead of scanning, and lists what it found", async () => {
    const posts: string[] = [];
    vi.stubGlobal("fetch", vi.fn(async (url: string, init?: RequestInit) => {
      if (init?.method === "POST") posts.push(url);
      const body =
        url === "/api/v1/discovery/status" ? { ready: true, why: "", librenms_url: "http://127.0.0.1:8000", mode: "semi-automatic", enabled: true, scan: null,
          last: { status: "done", at: null, error: "", result: { devices: 3, known: 2, found: 1, links: 4 } }, counts: { new: 1 } }
        : url === "/api/v1/discovery/candidates" ? [{ id: 9, ip: "10.0.0.9", hostname: "sw-edge-09", proposed_role: "Network device", open_ports: [], snmp_ok: true, status: "new", asset_id: "", seen_at: null, source: "librenms" }]
        : url === "/api/v1/discovery/scan" ? { queued: true, job_id: 5, status: "pending" }
        : url === "/api/v1/inventory-form" ? { types: [], extra_fields: [], environments: [], owners: [], sites: [], can_edit: true }
        : {};
      return new Response(JSON.stringify(body), { status: 200, headers: { "Content-Type": "application/json" } });
    }));
    render(<MemoryRouter><AddDevicesPage /></MemoryRouter>);
    fireEvent.click(screen.getByRole("tab", { name: "From LibreNMS" }));
    expect(await screen.findByText("sw-edge-09")).toBeTruthy();
    expect(screen.getByText(/3 devices, 2 known, 1 new/)).toBeTruthy();
    expect(screen.queryByText(/Ranges \(CIDR/)).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: "Read now" }));
    await waitFor(() => expect(posts).toContain("/api/v1/discovery/scan"));
  });
});
