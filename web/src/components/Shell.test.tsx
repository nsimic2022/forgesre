import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { afterEach, describe, expect, it } from "vitest";
import { Shell } from "./Shell";

describe("Main menu", () => {
  afterEach(() => (cleanup(), localStorage.removeItem("forgesre.navClosed")));

  it("folds away with the three-line button and comes back, remembered per browser", () => {
    const view = () => render(<MemoryRouter><Shell me={null} forgeai="off"><p>page</p></Shell></MemoryRouter>);
    view();
    expect(screen.getByRole("navigation", { name: "Main" })).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "Hide the menu" }));
    expect(screen.queryByRole("navigation", { name: "Main" })).toBeNull();
    expect(localStorage.getItem("forgesre.navClosed")).toBe("1");
    cleanup();
    view();
    expect(screen.queryByRole("navigation", { name: "Main" })).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: "Show the menu" }));
    expect(screen.getByRole("link", { name: "Incidents" })).toBeTruthy();
  });
});
