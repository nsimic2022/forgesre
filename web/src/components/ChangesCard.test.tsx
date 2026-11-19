import { cleanup, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it } from "vitest";
import { ChangesCard } from "./ChangesCard";

describe("What changed before", () => {
  afterEach(cleanup);
  it("lists changes with how long before the alarm, and links when there is a URL", () => {
    render(<ChangesCard changes={[
      { at: "2026-11-19T10:20:00Z", before_min: 40, source: "gitlab", kind: "deploy", title: "erp-api 4.2.1", detail: "", url: "https://ci.example/p/1", actor: "ci", asset: "db-ora-02" },
      { at: "2026-11-19T05:00:00Z", before_min: 360, source: "forgesre", kind: "config", title: "playrule update disk-db", detail: "", url: "", actor: "ana@dc.local", asset: "" },
    ]} />);
    expect(screen.getByText("What changed before · 2")).toBeTruthy();
    expect(screen.getByRole("link", { name: "erp-api 4.2.1" }).getAttribute("href")).toBe("https://ci.example/p/1");
    expect(screen.getByText("40 min before")).toBeTruthy();
    expect(screen.getByText("6 h 0 min before")).toBeTruthy();
  });
  it("explains how to feed it when empty", () => {
    render(<ChangesCard changes={[]} />);
    expect(screen.getByText("POST /api/v1/changes")).toBeTruthy();
  });
});
