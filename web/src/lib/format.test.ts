import { describe, expect, it } from "vitest";
import { duration, severityLabel, severityTone, shortNumber, tierLabel } from "./format";

describe("format", () => {
  it("shows durations the way the NOC reads them", () => {
    const now = new Date("2026-11-18T12:00:00Z");
    expect(duration("2026-11-18T11:54:00Z", now)).toBe("6 min");
    expect(duration("2026-11-18T10:20:00Z", now)).toBe("1 h 40 min");
    expect(duration("2026-11-16T09:00:00Z", now)).toBe("2 d 3 h");
    expect(duration(null, now)).toBe("—");
  });

  it("maps severities to tones and labels", () => {
    expect(severityTone("CRITICAL")).toBe("crit");
    expect(severityTone("high")).toBe("high");
    expect(severityTone("WARNING")).toBe("warn");
    expect(severityLabel("CRITICAL")).toBe("Critical");
  });

  it("shortens incident numbers and names escalation tiers", () => {
    expect(shortNumber("INC-0012_14.10.2026_22:15")).toBe("INC-0012");
    expect(shortNumber("INC-5100")).toBe("INC-5100");
    expect(tierLabel("L1")).toBe("Tier 1");
    expect(tierLabel(3)).toBe("Tier 3");
    expect(tierLabel(4)).toBe("Manager");
  });
});
