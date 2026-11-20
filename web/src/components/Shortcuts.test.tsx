import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { afterEach, describe, expect, it } from "vitest";
import { Shortcuts, titleFor } from "./Shortcuts";

function Page({ name }: { name: string }) {
  return <main><h1>{name}</h1><input type="search" aria-label="Search" /></main>;
}

describe("keyboard shortcuts", () => {
  afterEach(cleanup);

  it("g then a letter opens a page, / focuses search, ? shows the list, typing is ignored", () => {
    render(
      <MemoryRouter initialEntries={["/"]}>
        <Shortcuts />
        <Routes>
          <Route path="/" element={<Page name="NOC" />} />
          <Route path="/inventory" element={<Page name="Inventory" />} />
        </Routes>
      </MemoryRouter>,
    );
    fireEvent.keyDown(window, { key: "g" });
    fireEvent.keyDown(window, { key: "v" });
    expect(screen.getByRole("heading", { name: "Inventory" })).toBeTruthy();
    expect(document.title).toBe("Inventory · ForgeSRE");
    fireEvent.keyDown(window, { key: "/" });
    const box = screen.getByLabelText("Search");
    expect(document.activeElement).toBe(box);
    fireEvent.keyDown(box, { key: "?" });
    expect(screen.queryByRole("dialog")).toBeNull();
    fireEvent.keyDown(window, { key: "?" });
    expect(screen.getByRole("dialog", { name: "Keyboard shortcuts" })).toBeTruthy();
    fireEvent.keyDown(window, { key: "Escape" });
    expect(screen.queryByRole("dialog")).toBeNull();
  });

  it("names the browser tab after the page", () => {
    expect(titleFor("/incidents/INC-0412_19.11.2026_18:04")).toBe("INC-0412 · ForgeSRE");
    expect(titleFor("/inventory/add")).toBe("Add devices · ForgeSRE");
    expect(titleFor("/nowhere")).toBe("ForgeSRE");
  });
});
