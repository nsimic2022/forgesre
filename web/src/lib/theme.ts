/** Colour themes of the new UI. The choice is per browser (localStorage), like a wall screen that
 *  wants high contrast while desks keep the dark Signal style. */
export const THEMES: { id: string; label: string; note: string; swatch: string[] }[] = [
  { id: "signal", label: "Signal (dark)", note: "The default: near-black console, cyan accent.", swatch: ["#0a0b0d", "#171a20", "#4fc3f7", "#ff6b6b"] },
  { id: "light", label: "Light", note: "For bright rooms and printing screenshots.", swatch: ["#f4f6f8", "#ffffff", "#0369a1", "#c62828"] },
  { id: "midnight", label: "Midnight", note: "Dark blue, softer than black for long night shifts.", swatch: ["#0a0f1f", "#141d36", "#8ab4ff", "#ff6b6b"] },
  { id: "navy", label: "Navy", note: "The wall-screen colours: deep blue panels, yellow and soft blue accents.", swatch: ["#141d33", "#1f2a45", "#f7c52b", "#7aa7ff"] },
  { id: "contrast", label: "High contrast", note: "Black and pure colours, for wall screens and low vision.", swatch: ["#000000", "#161616", "#00e5ff", "#ff5c5c"] },
  { id: "system", label: "Follow the computer", note: "Light or dark as the operating system is set.", swatch: ["#f4f6f8", "#0a0b0d", "#4fc3f7", "#0369a1"] },
];
const KEY = "forgesre.theme";

export function savedTheme(): string {
  try {
    return localStorage.getItem(KEY) || "signal";
  } catch {
    return "signal";
  }
}

/** The theme this browser chose on purpose, or null when it never chose one. */
export function chosenTheme(): string | null {
  try {
    return localStorage.getItem(KEY);
  } catch {
    return null;
  }
}

/** Paint a theme without remembering it (the TV page uses this for ?theme= and its Navy default). */
export function showTheme(id: string): string {
  const theme = THEMES.some((t) => t.id === id) ? id : "signal";
  if (theme === "signal") delete document.documentElement.dataset.theme;
  else document.documentElement.dataset.theme = theme;
  return theme;
}

export function applyTheme(id: string): void {
  const theme = showTheme(id);
  try {
    localStorage.setItem(KEY, theme);
  } catch {
    /* private mode: the theme lasts until the tab closes */
  }
}
