import { cleanup, render, screen } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { afterEach, describe, expect, it } from "vitest";
import { Shell } from "./Shell";

afterEach(cleanup);

describe("Shell", () => {
  it("shows the appliance version from /me, and nothing when it is unknown", () => {
    const { rerender } = render(
      <MemoryRouter>
        <Shell me={{ email: "a@x", role: "admin", name: "Ana", version: "2.0.0" }} forgeai="off">x</Shell>
      </MemoryRouter>,
    );
    expect(screen.getByText("2.0")).toBeTruthy();
    rerender(
      <MemoryRouter>
        <Shell me={{ email: "a@x", role: "admin", name: "Ana" }} forgeai="off">x</Shell>
      </MemoryRouter>,
    );
    expect(screen.queryByText(/^\d+\.\d+$/)).toBeNull();
  });
});
