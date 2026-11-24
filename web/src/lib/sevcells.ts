/**
 * Severity colour on the incident name ("Problem") cell, per browser: the cell is tinted in the
 * severity's colour and the text takes that colour, so the level shows at a glance. On by default;
 * Settings › Appearance turns it off. Painted as data-sevcells on <html>, read by styles.css.
 */
const KEY = "forgesre.sevcells";

export function sevCellsOn(): boolean {
  try {
    return localStorage.getItem(KEY) !== "off";
  } catch {
    return true;
  }
}

export function paintSevCells(on: boolean = sevCellsOn()): void {
  document.documentElement.dataset.sevcells = on ? "on" : "off";
}

export function saveSevCells(on: boolean): void {
  try {
    localStorage.setItem(KEY, on ? "on" : "off");
  } catch {
    /* private mode: lasts until the tab closes */
  }
  paintSevCells(on);
}

/** Class names for a cell (or span) that shows an incident's name. */
export function sevClass(severity: string | null | undefined): string {
  const s = String(severity || "").toLowerCase();
  return ["critical", "high", "warning"].includes(s) ? `sev-cell sev-${s}` : "sev-cell";
}
