import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it } from "vitest";
import { IncidentStory } from "./IncidentPanels";

describe("IncidentStory", () => {
  afterEach(() => (cleanup(), window.localStorage.clear()));

  it("pages a long story like every table", () => {
    const events = Array.from({ length: 23 }, (_, i) => ({ at: "2026-11-26T08:00:00Z", kind: "notify", text: `Mail ${i + 1}`, who: "" }));
    render(<IncidentStory events={events} />);
    expect(screen.getByText("23 events")).toBeTruthy();
    expect(screen.getByText("Mail 10")).toBeTruthy();
    expect(screen.queryByText("Mail 11")).toBeNull();
    expect(screen.getByText("1–10 of 23")).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "Next page" }));
    expect(screen.getByText("Mail 11")).toBeTruthy();
  });
});
