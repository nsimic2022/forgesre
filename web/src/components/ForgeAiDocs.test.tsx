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
    expect(await screen.findByText("match 82%")).toBeTruthy();
    expect(screen.getByText(/Dell R750 manual · page 114/)).toBeTruthy();
    expect(screen.getByText("900", { selector: ".stat-value" })).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "Remove" }));
    await waitFor(() => expect(calls).toContain("DELETE /api/v1/forgeai/docs/4"));
  });

  it("explains a stopped embedding model, uploads several files and re-indexes a failed one", async () => {
    const calls: string[] = [];
    vi.stubGlobal("fetch", vi.fn(async (url: string, init?: RequestInit) => {
      calls.push(`${init?.method ?? "GET"} ${url}`);
      const body = url.includes("/reindex") ? { queued: true }
        : { docs: [{ id: 7, name: "bad.pdf", title: "Scanned", size: 4096, pages: 0, chunks: 0, status: "error", error: "no text layer", model: "", uploaded_by: "e@dc.local", at: null }],
            embedder: false, embed_url: "", model: "nomic", pgvector: false, can_edit: true, usage: { days: 7, given: 3, cited: 1 } };
      return new Response(JSON.stringify(body), { status: 200, headers: { "Content-Type": "application/json" } });
    }));
    render(<ForgeAiDocs />);
    expect(await screen.findByText(/The embedding model does not answer/)).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "Index again" }));
    await waitFor(() => expect(calls).toContain("POST /api/v1/forgeai/docs/7/reindex"));
    const files = [new File(["a"], "a.md"), new File(["b"], "b.md")];
    fireEvent.change(screen.getByLabelText("Document files"), { target: { files } });
    await waitFor(() => expect(calls.filter((c) => c.startsWith("POST /api/v1/forgeai/docs?")).length).toBe(2));
  });

  it("marks the query words in a hit", async () => {
    const { highlight } = await import("./ForgeAiDocs");
    const { container } = render(<span>{highlight("Replace the failed disk now", "failed disk")}</span>);
    expect([...container.querySelectorAll("mark")].map((m) => m.textContent)).toEqual(["failed", "disk"]);
  });
});
