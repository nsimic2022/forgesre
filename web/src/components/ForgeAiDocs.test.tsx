import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { ForgeAiDocs } from "./ForgeAiDocs";

describe("ForgeAI documents", () => {
  afterEach(() => (cleanup(), vi.unstubAllGlobals()));

  it("lists documents, searches them and lets an Engineer remove one", async () => {
    const calls: string[] = [];
    vi.stubGlobal("fetch", vi.fn(async (url: string, init?: RequestInit) => {
      calls.push(`${init?.method ?? "GET"} ${url}`);
      const body = url.startsWith("/api/v1/forgeai/docs/search")
        ? { hits: [{ chunk: 1, doc: "Dell R750 manual", page: 114, text: "Replace the failed disk and let the rebuild finish.", score: 0.82 }] }
        : url === "/api/v1/forgeai/docs/4" ? { deleted: 4 }
        : { docs: [{ id: 4, name: "r750.pdf", title: "Dell R750 manual", size: 2048, pages: 300, chunks: 900, status: "ready", error: "", model: "nomic", uploaded_by: "e@dc.local", at: null }],
            embedder: true, embed_url: "http://127.0.0.1:8089/v1", model: "nomic-embed-text-v1.5", pgvector: true, can_edit: true };
      return new Response(JSON.stringify(body), { status: 200, headers: { "Content-Type": "application/json" } });
    }));
    render(<ForgeAiDocs />);
    expect(await screen.findByText("Dell R750 manual")).toBeTruthy();
    expect(screen.getByText("300 pages · 900 pieces")).toBeTruthy();
    fireEvent.change(screen.getByLabelText("Try the document search"), { target: { value: "degraded RAID" } });
    fireEvent.click(screen.getByRole("button", { name: "Search" }));
    expect(await screen.findByText(/page 114 · match 82%/)).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "Remove" }));
    await waitFor(() => expect(calls).toContain("DELETE /api/v1/forgeai/docs/4"));
  });
});
