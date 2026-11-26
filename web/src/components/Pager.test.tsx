import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { Pager } from "./Pager";

describe("Pager", () => {
  afterEach(cleanup);

  it("shows from the first row, also when everything fits on one page", () => {
    render(<Pager total={5} offset={0} size={10} onOffset={() => undefined} onSize={() => undefined} label="incidents" />);
    expect(screen.getByRole("navigation", { name: "Pages of incidents" })).toBeTruthy();
    expect(screen.getByText("1–5 of 5")).toBeTruthy();
    expect((screen.getByRole("button", { name: "Next page" }) as HTMLButtonElement).disabled).toBe(true);
  });

  it("is not shown for an empty list, and moves between pages", () => {
    const { container } = render(<Pager total={0} offset={0} size={10} onOffset={() => undefined} onSize={() => undefined} />);
    expect(container.innerHTML).toBe("");
    cleanup();
    const onOffset = vi.fn();
    render(<Pager total={92} offset={0} size={10} onOffset={onOffset} onSize={() => undefined} label="incidents" />);
    fireEvent.click(screen.getByRole("button", { name: "Next page" }));
    expect(onOffset).toHaveBeenCalledWith(10);
    expect(screen.getByText("1–10 of 92")).toBeTruthy();
  });
});
