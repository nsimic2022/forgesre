import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { BackupPanel, UsersPanel } from "./AdminPanels";

const roles = [
  { key: "viewer", label: "Viewer", help: "Viewer — read only" },
  { key: "analyst", label: "Analyst", help: "Analyst — NOC operator" },
  { key: "engineer", label: "Engineer", help: "Engineer — changes everything operational" },
  { key: "admin", label: "Admin", help: "Admin — also settings, users, backup" },
];
const users = [
  { id: 1, email: "admin@dc.local", email2: "", name: "Nenad", surname: "S.", phone: "", phone2: "", role: "admin", active: true, me: true, last_admin: true, delete_blocked: "cannot remove yourself" },
  { id: 2, email: "ana@dc.local", email2: "", name: "Ana", surname: "P.", phone: "+381 60 1", phone2: "", role: "analyst", active: true, me: false, last_admin: false, delete_blocked: "" },
];

describe("Admin panels", () => {
  let calls: { url: string; method: string; body: unknown }[];
  beforeEach(() => {
    calls = [];
    vi.stubGlobal("fetch", vi.fn(async (url: string, init?: RequestInit) => {
      calls.push({ url, method: init?.method ?? "GET", body: typeof init?.body === "string" ? JSON.parse(init.body) : init?.body ?? null });
      const body = url === "/api/v1/users-page" ? { users, roles }
        : url === "/api/v1/backups" && (init?.method ?? "GET") === "GET" ? { archives: [{ name: "backup_20261119T100000Z", size: 2048, mtime: "2026-11-19T10:00:00Z", label: "19 Nov 2026 10:00 · 2 KB" }], running: false, last_job: null }
        : { ok: true, job: 5, status: "pending" };
      return new Response(JSON.stringify(body), { status: 200, headers: { "Content-Type": "application/json" } });
    }));
  });
  afterEach(() => (cleanup(), vi.unstubAllGlobals()));

  it("protects yourself and promotes a user", async () => {
    render(<UsersPanel />);
    expect(await screen.findByText("cannot remove yourself")).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "Edit user ana@dc.local" }));
    fireEvent.click(screen.getByLabelText(/Engineer — changes/));
    fireEvent.click(screen.getByRole("button", { name: "Save user" }));
    await waitFor(() => expect(calls.some((c) => c.method === "POST" && c.url === "/api/v1/users/2")).toBe(true));
    const body = calls.find((c) => c.url === "/api/v1/users/2")!.body as Record<string, string>;
    expect(body.role).toBe("engineer");
    expect(body.password).toBeUndefined();
  });

  it("does not let the last admin be demoted in the form", async () => {
    render(<UsersPanel />);
    fireEvent.click(await screen.findByRole("button", { name: "Edit user admin@dc.local" }));
    expect((screen.getByLabelText(/Viewer — read only/) as HTMLInputElement).disabled).toBe(true);
  });

  it("starts a background backup and removes one after asking", async () => {
    render(<BackupPanel />);
    expect(await screen.findByText("19 Nov 2026 10:00 · 2 KB")).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "Back up now" }));
    await waitFor(() => expect(calls.some((c) => c.method === "POST" && c.url === "/api/v1/backups")).toBe(true));
    expect(calls.find((c) => c.method === "POST")!.body).toEqual({ include_secrets: true, include_models: false });
    fireEvent.click(screen.getByRole("button", { name: "Remove backup backup_20261119T100000Z" }));
    fireEvent.click(screen.getByRole("button", { name: "Yes, remove" }));
    await waitFor(() => expect(calls.some((c) => c.method === "DELETE" && c.url === "/api/v1/backups/backup_20261119T100000Z")).toBe(true));
  });
});
