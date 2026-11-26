import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { MetricTile, segments, since, type MetricTileData } from "./MetricTile";

const times = Array.from({ length: 11 }, (_, i) => 1000 + i * 60);
const win = { start: 1000, end: 1600, marker: 1300, label: "x" };
const cpu: MetricTileData = { key: "cpu", name: "CPU", kind: "percent", display: "88%", tone: "ok", threshold: 90, series: times.map((_, i) => 40 + i * 4.8), times };

describe("MetricTile", () => {
  afterEach(cleanup);

  it("splits lines at missing samples and says how far back the window goes", () => {
    expect(segments([[0, 1], [60, 2], [120, 3], [600, 4], [660, 5]]).map((s) => s.length)).toEqual([3, 2]);
    expect(since(86400)).toBe("−24 h");
    expect(since(3600)).toBe("−60 min");
    expect(since(7 * 86400)).toBe("−7 d");
  });

  it("shows the value, change, min / avg / max, and amber when close to the alarm line", () => {
    render(<MetricTile tile={cpu} window={win} hover={null} onHover={() => undefined} />);
    expect(screen.getByText("88")).toBeTruthy();
    expect(screen.getByText(/▲ 48 %/)).toBeTruthy();
    expect(screen.getByText("min 40")).toBeTruthy();
    expect(screen.getByText("max 88")).toBeTruthy();
    expect(screen.getByText("88").closest(".metric-value")?.getAttribute("data-tone")).toBe("warn");
    expect(screen.getByRole("img", { name: "CPU over the window, now 88%" })).toBeTruthy();
  });

  it("reports the cursor time and shows the value there when another tile is hovered", () => {
    const onHover = vi.fn();
    const { rerender } = render(<MetricTile tile={cpu} window={win} hover={null} onHover={onHover} />);
    const svg = screen.getByRole("img");
    svg.getBoundingClientRect = () => ({ left: 0, width: 600, top: 0, height: 80, right: 600, bottom: 80, x: 0, y: 0, toJSON: () => ({}) });
    fireEvent.mouseMove(svg, { clientX: 300 });
    expect(onHover).toHaveBeenCalledWith(1300);
    rerender(<MetricTile tile={cpu} window={win} hover={1300} onHover={onHover} />);
    expect(screen.getByText(/· 64 %/)).toBeTruthy();
  });

  it("draws up / down as a strip with availability, and names extra lines", () => {
    const up: MetricTileData = { key: "up", name: "Collecting", kind: "up", display: "up", tone: "ok", threshold: null, series: times.map((_, i) => (i === 4 ? 0 : 1)), times };
    render(<MetricTile tile={up} window={win} hover={null} onHover={() => undefined} />);
    expect(screen.getByText(/available 9\d/)).toBeTruthy();
    cleanup();
    const net: MetricTileData = { key: "network", name: "NET", kind: "bytes", unit: "MB/s", display: "4.8 MB/s", tone: "ok", threshold: null, series: cpu.series, times,
      lines: [{ name: "RX", series: cpu.series, times }, { name: "TX", series: cpu.series.map((v) => v / 2), times }] };
    render(<MetricTile tile={net} window={win} hover={null} onHover={() => undefined} />);
    expect(screen.getByText("RX")).toBeTruthy();
    expect(screen.getByText("TX")).toBeTruthy();
  });
});
