/*
 * The incident list the user last looked at (tab, filters, page), kept in this browser tab, so the
 * incident page can step to the previous / next incident of that same list and "Back to incidents"
 * returns to the same tab and page.
 */

export type IncidentListContext = {
  tab: string; severity: string; q: string; date_from: string; date_to: string; team: string; offset: number;
};

const KEY = "forgesre.incidentList";
export const DEFAULT_LIST: IncidentListContext = { tab: "active", severity: "", q: "", date_from: "", date_to: "", team: "", offset: 0 };

export function loadIncidentList(): IncidentListContext {
  try {
    const raw = window.sessionStorage.getItem(KEY);
    return raw ? { ...DEFAULT_LIST, ...(JSON.parse(raw) as Partial<IncidentListContext>) } : DEFAULT_LIST;
  } catch {
    return DEFAULT_LIST; // storage off or broken: the default list is fine
  }
}

export function saveIncidentList(ctx: IncidentListContext): void {
  try {
    window.sessionStorage.setItem(KEY, JSON.stringify(ctx));
  } catch {
    /* not remembered; the arrows then use the default list */
  }
}

export const TAB_LABEL: Record<string, string> = { active: "Not acknowledged", acknowledged: "Acknowledged", resolved: "Resolved", all: "All" };
