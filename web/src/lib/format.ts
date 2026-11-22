// Small display helpers shared by pages.

export type Tone = "crit" | "high" | "warn" | "ok" | "info" | "off";

export function severityTone(severity: string): Tone {
  const s = severity.toUpperCase();
  if (s === "CRITICAL") return "crit";
  if (s === "HIGH") return "high";
  if (s === "WARNING") return "warn";
  return "info";
}

export function severityLabel(severity: string): string {
  const s = severity.toLowerCase();
  return s.charAt(0).toUpperCase() + s.slice(1);
}

/** "6 min", "1 h 40 min", "2 d 3 h" between two instants (now by default). */
export function duration(fromIso: string | null, to: Date = new Date()): string {
  if (!fromIso) return "—";
  const minutes = Math.max(0, Math.floor((to.getTime() - new Date(fromIso).getTime()) / 60000));
  if (minutes < 60) return `${minutes} min`;
  const hours = Math.floor(minutes / 60);
  if (hours < 24) return minutes % 60 ? `${hours} h ${minutes % 60} min` : `${hours} h`;
  const days = Math.floor(hours / 24);
  return hours % 24 ? `${days} d ${hours % 24} h` : `${days} d`;
}

/** Local HH:MM for today, else "18 Nov HH:MM". */
export function clock(iso: string | null, now: Date = new Date()): string {
  if (!iso) return "—";
  const d = new Date(iso);
  const hm = d.toLocaleTimeString("en-GB", { hour: "2-digit", minute: "2-digit" });
  if (d.toDateString() === now.toDateString()) return hm;
  return `${d.toLocaleDateString("en-GB", { day: "numeric", month: "short" })} ${hm}`;
}

export function minutesLabel(value: number | null): string {
  if (value === null || value === undefined) return "—";
  return value < 60 ? `${Math.round(value)} min` : duration(new Date(Date.now() - value * 60000).toISOString());
}

export const FORGEAI_LABEL: Record<string, { text: string; tone: Tone }> = {
  "ready-local": { text: "Ready · local", tone: "ok" },
  "ready-external": { text: "Ready · external", tone: "info" },
  running: { text: "Running", tone: "warn" },
  queued: { text: "Queued", tone: "off" },
  off: { text: "Off", tone: "off" },
  none: { text: "—", tone: "off" },
};

/** "INC-0012_14.10.2026_22:15" → "INC-0012": tables already show the date in a column of their own. */
export function shortNumber(number: string): string {
  return number.split("_")[0];
}

/** Escalation step names in the new UI: three support tiers, then the manager (not a tier).
 *  The backend keeps levels 1–4 (classic shows L1–L4). Accepts 2 or "L2". */
export function tierLabel(level: number | string | null | undefined): string {
  const n = typeof level === "number" ? level : Number(String(level ?? "").replace(/^L/i, ""));
  if (!n) return String(level ?? "");
  return `Tier ${n}`;
}

/** Who a tier reaches by default: Tier 1–3 the on-call ladder, Tier 4 the manager. For tooltips. */
export function tierRole(level: number | string | null | undefined): string {
  const n = typeof level === "number" ? level : Number(String(level ?? "").replace(/^L/i, ""));
  return n >= 4 ? "Manager" : n ? `On-call ladder step ${n}` : "";
}
