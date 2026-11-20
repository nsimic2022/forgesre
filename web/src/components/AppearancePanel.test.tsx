import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it } from "vitest";
import { AppearancePanel } from "./AppearancePanel";

describe("AppearancePanel", () => {
  afterEach(() => (cleanup(), delete document.documentElement.dataset.theme));

  it("applies and remembers a theme", () => {
    render(<AppearancePanel />);
    fireEvent.click(screen.getByRole("radio", { name: /High contrast/ }));
    expect(document.documentElement.dataset.theme).toBe("contrast");
    expect(localStorage.getItem("forgesre.theme")).toBe("contrast");
    fireEvent.click(screen.getByRole("radio", { name: /Signal/ }));
    expect(document.documentElement.dataset.theme).toBeUndefined();
  });
});
