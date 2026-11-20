import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { ImportCsv } from "./ImportCsv";

const preview = {
  counts: { create: 1, exists: 1, error: 1 }, ignored_columns: ["colour"], columns: ["hostname"],
  rows: [
    { line: 2, hostname: "web-01", ip: "10.0.0.11", type: "Linux Server", asset_id: "web-01", action: "create", reason: "" },
    { line: 3, hostname: "db-01", ip: "10.0.0.12", type: "Linux Server", asset_id: "db-01", action: "exists", reason: "already in the inventory as db-01; not changed" },
    { line: 4, hostname: "bad", ip: "10.0.0.13:9100", type: "", asset_id: "", action: "error", reason: "IP is the address only (no :port)" },
  ],
};

describe("ImportCsv", () => {
  afterEach(() => (cleanup(), vi.unstubAllGlobals()));

  it("checks the file first, then adds the accepted rows in a job", async () => {
    const posts: { url: string; body: { csv: string } }[] = [];
    vi.stubGlobal("fetch", vi.fn(async (url: string, init?: RequestInit) => {
      if (init?.method === "POST") posts.push({ url, body: JSON.parse(String(init.body)) });
      const body = url.endsWith("/preview") ? preview : url === "/api/v1/assets/import" ? { job: 7, counts: preview.counts }
        : { id: 7, kind: "asset_import", status: "done", error: "", result: { created: ["web-01"], failed: [] } };
      return new Response(JSON.stringify(body), { status: 200, headers: { "Content-Type": "application/json" } });
    }));
    const imported = vi.fn();
    render(<ImportCsv onClose={() => undefined} onImported={imported} />);
    fireEvent.change(screen.getByLabelText("CSV text"), { target: { value: "hostname,ip\nweb-01,10.0.0.11" } });
    fireEvent.click(screen.getByRole("button", { name: "Check file" }));
    expect(await screen.findByText("IP is the address only (no :port)")).toBeTruthy();
    expect(screen.getByText(/ignored columns: colour/)).toBeTruthy();
    expect(posts[0]).toEqual({ url: "/api/v1/assets/import/preview", body: { csv: "hostname,ip\nweb-01,10.0.0.11" } });
    fireEvent.click(screen.getByRole("button", { name: "Add 1 asset" }));
    expect(await screen.findByText(/Added 1 asset\./)).toBeTruthy();
    expect(imported).toHaveBeenCalledWith(1);
    expect(posts[1].url).toBe("/api/v1/assets/import");
  });
});
