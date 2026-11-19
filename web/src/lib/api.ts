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

export async function postJson<T>(path: string, body: unknown): Promise<T> {
  return getJson<T>(path, {
    method: "POST",
    headers: { Accept: "application/json", "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
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

export interface IncidentPage {
  tab: string;
  counts: Record<"active" | "acknowledged" | "resolved" | "all", number>;
  total: number;
  offset: number;
  limit: number;
  rows: NocIncident[];
}

export interface Fact {
  id: string;
  text: string;
  kind?: string;
}

export interface Hypothesis {
  id: string;
  summary: string;
  confidence: number;
  rank: number;
  evidence_count?: number;
}

export interface IncidentView {
  number: string;
  title: string;
  severity: string;
  status: string;
  source: string;
  summary: string;
  started_at: string | null;
  ack_at: string | null;
  ack_by: string;
  resolved_at: string | null;
  resolved_by: string;
  level: string;
  levels: { level: number; tone: string; title: string }[];
  asset: null | {
    hostname: string;
    ip: string;
    type: string;
    environment: string;
    owner: string;
    contact_name: string;
    owner_email: string;
    owner_phone: string;
    notes: string;
    glpi_id: string;
    zabbix_hostid: string;
  };
  playrule: string | null;
  playbook: null | { name: string; steps: string[]; done: number[] };
  forgerca: null | {
    cause: string;
    summary: string;
    confidence: number;
    facts: Fact[];
    hypotheses: Hypothesis[];
    actions: string[];
    limitations: string[];
    at: string | null;
  };
  forgeai: { mode: string; text: string; cause: string; action: string; model: string; at: string | null };
  evidence: { id: string; source: string; title: string; query: string }[];
  timeline: { at?: string; event?: string; text?: string; [key: string]: unknown }[];
  notifications: { id: number; target: string; subject: string; status: string; step_key: string; channel: string; created_at: string | null; error: string }[];
  notes: { id: number; at: string | null; actor: string; body: string }[];
  can: { work: boolean; investigate: boolean; cloud: boolean };
}
