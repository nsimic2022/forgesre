// Thin fetch wrapper for Core's JSON API. Same origin, session cookie; a 401 sends the
// browser to the classic sign-in page.

export class ApiError extends Error {
  constructor(public status: number, message: string) {
    super(message);
  }
}

export async function getJson<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(path, { credentials: "same-origin", headers: { Accept: "application/json" }, ...init });
  if (response.status === 401) {
    window.location.assign("/login");
    throw new ApiError(401, "not signed in");
  }
  if (!response.ok) {
    let detail = response.statusText;
    try {
      detail = (await response.json()).detail ?? detail;
    } catch {
      /* body was not JSON */
    }
    throw new ApiError(response.status, String(detail));
  }
  return (await response.json()) as T;
}

export type Role = "viewer" | "analyst" | "engineer" | "admin";

export interface Me {
  email: string;
  role: Role;
  name: string;
}

export type Severity = "CRITICAL" | "HIGH" | "WARNING" | string;
export type ForgeAiState = "off" | "none" | "queued" | "running" | "ready-local" | "ready-external";

export interface NocIncident {
  number: string;
  severity: Severity;
  status: string;
  title: string;
  detail: string;
  source: string;
  started_at: string | null;
  ack_at: string | null;
  ack_by: string;
  resolved_at: string | null;
  asset: { hostname: string; type: string; ip: string } | null;
  level: string;
  messages: number;
  forgerca: string;
  forgeai: ForgeAiState;
}

export interface Noc {
  generated_at: string;
  counts: {
    open: number;
    critical: number;
    high: number;
    warning: number;
    not_acked: number;
    hosts_total: number;
    hosts_down: number;
    hosts_down_names: string[];
    mtta_today_min: number | null;
    mttr_7d_min: number | null;
  };
  incidents: { active: NocIncident[]; acknowledged: NocIncident[]; resolved_today: NocIncident[] };
  health: { group: string; up: number; total: number }[];
  oncall: { name: string; email: string; phone: string } | null;
  ai: { forgerca: boolean; forgeai: "off" | "local" | "external" };
}
