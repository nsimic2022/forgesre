import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { SendReport } from "./SendReport";

const draft = {
  subject: "Alarm web-01", body: "INC-0412 HighCPU on web-01", smtp: true,
  suggestions: [{ email: "owner@dc.local", label: "Owner of web-01" }], sent: [],
};

describe("SendReport", () => {
  afterEach(() => (cleanup(), vi.unstubAllGlobals()));

  it("fills the generated report, adds a suggestion and shows the result per address", async () => {
    const posts: unknown[] = [];
    vi.stubGlobal("fetch", vi.fn(async (url: string, init?: RequestInit) => {
      if (init?.method === "POST") posts.push(JSON.parse(String(init.body)));
      const body = url.endsWith("/mail") && init?.method === "POST" ? { job: 3 }
        : url.endsWith("/mail/3") ? { status: "done", error: "", result: [{ to: "owner@dc.local", status: "sent", error: "" }] }
        : draft;
      return new Response(JSON.stringify(body), { status: 200, headers: { "Content-Type": "application/json" } });
    }));
    render(<SendReport number="INC-0412" onClose={() => undefined} />);
    expect(((await screen.findByLabelText("Report")) as HTMLTextAreaElement).value).toBe("INC-0412 HighCPU on web-01");
    fireEvent.click(screen.getByRole("button", { name: "+ owner@dc.local" }));
    fireEvent.click(screen.getByRole("button", { name: "Send" }));
    expect(await screen.findByText("sent")).toBeTruthy();
    expect(posts[0]).toEqual({ to: "owner@dc.local", subject: "Alarm web-01", body: "" });
  });

  it("warns when SMTP is off", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => new Response(JSON.stringify({ ...draft, smtp: false }), { status: 200, headers: { "Content-Type": "application/json" } })));
    render(<SendReport number="INC-0412" onClose={() => undefined} />);
    expect(await screen.findByText(/SMTP is off/)).toBeTruthy();
  });
});
