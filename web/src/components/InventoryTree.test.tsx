import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { InventoryTree, type TreeNode } from "./InventoryTree";

const asset = (id: string, state: string, children: TreeNode[] = []): TreeNode => ({ kind: "asset", name: id, id, type: "Linux Server", ip: "10.0.0.1", state, open: 0, assets: 1, open_total: 0, worst: state, children });
const tree: TreeNode = { kind: "root", name: "All", assets: 3, open_total: 0, worst: "down", children: [
  { kind: "site", name: "DC1", assets: 3, open_total: 0, worst: "down", children: [
    { kind: "row", name: "A", assets: 3, open_total: 0, worst: "down", children: [
      { kind: "rack", name: "A01", assets: 3, open_total: 0, worst: "down", children: [asset("esx-01", "up", [asset("vm-web", "down")]), asset("sw-01", "up")] }] }] }] };

describe("inventory tree", () => {
  afterEach(() => (cleanup(), vi.unstubAllGlobals()));

  it("opens the path to a search hit and picks an asset", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => new Response(JSON.stringify(tree), { status: 200, headers: { "Content-Type": "application/json" } })));
    const pick = vi.fn();
    const { rerender } = render(<InventoryTree query="" selected="" onPick={pick} reload={0} />);
    expect(await screen.findByText("DC1")).toBeTruthy();
    rerender(<InventoryTree query="vm-web" selected="" onPick={pick} reload={0} />);
    fireEvent.click(await screen.findByText("vm-web"));
    expect(pick).toHaveBeenCalledWith("vm-web");
    expect(screen.queryByText("sw-01")).toBeNull();
  });
});
