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

export async function putJson<T>(path: string, body: unknown): Promise<T> {
  return getJson<T>(path, {
    method: "PUT",
    headers: { Accept: "application/json", "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
}

export type Role = "viewer" | "analyst" | "engineer" | "admin";

export interface Me {
  email: string;
  role: Role;
  name: string;
  version?: string;
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
  flap_count?: number;
  caused_by?: string;
  sla?: ({ kind: "response" | "resolve" } & SlaClock) | null;
  major?: boolean;
}

export interface Noc {
  generated_at: string;
  major?: MajorBrief[];
  handover?: Handover | null;
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

export interface ChangeRow {
  at: string;
  before_min: number;
  source: string;
  kind: string;
  title: string;
  detail: string;
  url: string;
  actor: string;
  asset: string;
}

export interface TopoNode {
  asset_id: string;
  hostname: string;
  type: string;
  ip: string;
  alarm: boolean;
  network: boolean;
  behind: number;
  children: TopoNode[];
}

export interface Topology {
  roots: TopoNode[];
  unlinked: TopoNode[];
  assets: number;
  with_parent: number;
  links: number;
  last_sync: string | null;
  suggestions: { asset_id: string; hostname: string; parent_asset_id: string; parent_hostname: string; current_parent: string; protocol: string; port: string }[];
  can_edit: boolean;
}

export interface ClockHour {
  start: string;
  hour: number;
  critical: number;
  high: number;
  warning: number;
}

export interface IncidentClockData {
  now: string;
  hour: number;
  minute: number;
  timezone: string;
  hours: ClockHour[];
  total: number;
}

export type DayHour = { hour: number; critical: number; high: number; warning: number };
export type TvDayDetail = { date: string; label: string; timezone: string; am: DayHour[]; pm: DayHour[]; total: number; critical: number; high: number; warning: number };
export type TvDay = { date: string; label: string; day: number; incidents: number; critical: number; high: number; worst: string; mttr_min: number | null };
export type TvHour = { at: string; hour: number; open: number; new: number; critical: number; high: number; acked_fast_pct: number | null };
export type TvTile = {
  key: string; title: string; value: number | string; unit: string; word: string; trend: "up" | "down" | "steady"; sentence: string;
  tone: "ok" | "warn" | "crit" | "info" | "off"; gauge: number | null; detail: string[]; start?: string; end?: string;
  bars?: { label: string; value: number }[];
};
export type TvData = Noc & {
  clock: IncidentClockData; daily?: { day: string; critical: number; high: number; other: number }[];
  days?: TvDay[];
  hours?: { from: string; to: string; points: TvHour[]; maintenance: { from: string; to: string; asset: string; summary: string }[] };
  tiles?: TvTile[];
};

export interface IncidentPage {
  tab: string;
  counts: Record<"active" | "acknowledged" | "resolved" | "all", number>;
  total: number;
  offset: number;
  limit: number;
  rows: NocIncident[];
  teams?: string[];
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
    asset_id: string;
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
  forgeai: import("../components/ForgeAiCard").ForgeAiView;
  evidence: { id: string; source: string; title: string; query: string }[];
  timeline: { at?: string; event?: string; text?: string; [key: string]: unknown }[];
  changes?: ChangeRow[];
  notifications: { id: number; target: string; subject: string; status: string; step_key: string; channel: string; created_at: string | null; error: string }[];
  notes: { id: number; at: string | null; actor: string; body: string }[];
  events?: { at: string; kind: string; who: string; text: string }[];
  who_to_call?: { role: string; name: string; phone: string; email: string; note?: string }[];
  similar?: { number: string; status: string; started_at: string | null; minutes: number | null; root_cause: string; resolved_by: string }[];
  review: Review;
  links?: { caused_by: IncidentBrief | null; consequences: IncidentBrief[] };
  major?: MajorView;
  sla?: SlaState | null;
  actions?: FollowUp[];
  ticket?: { ref: string; url: string; status: string } | null;
  can_ticket?: boolean;
  known_errors?: KnownError[];
  alertname?: string;
  can_known_error?: boolean;
  can: { work: boolean; investigate: boolean; cloud: boolean; quiet?: boolean };
}

export type MajorView = { on: boolean; commander: string; bridge_url: string; update_minutes: number; last_update_at: string | null; next_due: string | null; overdue: boolean; message: string };
export type SlaClock = { state: "none" | "running" | "met" | "breached"; due?: string; target_min?: number; minutes_left?: number };
export type SlaState = { response: SlaClock; resolve: SlaClock };
export type FollowUp = { id: number; text: string; owner: string; due: string | null; done: boolean; done_at: string | null; created_by: string; overdue: boolean; incident?: string; incident_title?: string };
export type Handover = { id: number; created_by: string; created_at: string | null; notes: string; open_incidents: { number: string; title: string; severity: string; acked: boolean }[]; taken_by: string; taken_at: string | null };
export type MajorBrief = { number: string; title: string; commander: string; bridge_url: string; next_due: string | null; overdue: boolean };

export type IncidentBrief = { number: string; title: string; status: string; severity: string };

export type Rating = "" | "correct" | "partly" | "wrong";

export interface Review {
  root_cause: string;
  rca_rating: Rating;
  ai_rating: Rating;
  reviewed_by: string;
  reviewed_at: string | null;
}

export interface Accuracy {
  rated: number;
  correct: number;
  partly: number;
  wrong: number;
  score: number | null;
}

export interface InvestigationsPage {
  days: number;
  total: number;
  offset: number;
  limit: number;
  rows: ({ number: string; title: string; severity: string; started_at: string | null; minutes: number | null; host: string; forgerca: string; forgeai: boolean } & Review)[];
  accuracy: { forgerca: Accuracy; forgeai: Accuracy };
  reviewed: number;
  resolved: number;
  recurring: { cause: string; count: number; hosts: string[] }[];
}

export interface LibraryItem {
  name: string;
  count: number;
}

export interface InventoryRow {
  asset_id: string;
  number: number;
  hostname: string;
  ip: string;
  type: string;
  environment: string;
  site: string;
  row: string;
  rack: string;
  customer: string;
  owner: string;
  state: "down" | "alarm" | "up" | "unknown";
  sources: string[];
  open: number;
}

export interface InventoryPage {
  library: {
    sites: (LibraryItem & { racks: LibraryItem[] })[];
    types: LibraryItem[];
    environments: LibraryItem[];
    customers: LibraryItem[];
    views: (LibraryItem & { key: string })[];
  };
  total: number;
  all: number;
  offset: number;
  limit: number;
  rows: InventoryRow[];
  can_edit?: boolean;
}

export interface AssetCard extends InventoryRow {
  monitoring_profile: string;
  contact_name: string;
  owner_email: string;
  owner_phone: string;
  notes: string;
  glpi_id: string;
  zabbix_hostid: string;
  scrape_address: string;
  parent: string;
  details: { key: string; label: string; value: string }[];
  incidents: { number: string; title: string; severity: string; status: string; started_at: string | null }[];
  can_edit: boolean;
  snmp_port?: number;
  extras?: Record<string, string | boolean>;
  snmp?: { snmp_version: string; snmp_community_mode: string; snmp_v3_user: string; desired: string; effective: string; pending: boolean } | null;
}

export interface AssetFormOptions {
  types: { group: string; names: string[] }[];
  extra_fields: { key: string; label: string; max: number }[];
  environments: string[];
  owners: string[];
  sites: string[];
  can_edit: boolean;
}

export interface PolicyStep {
  after_minutes: number;
  target: string;
  channel: string;
  level?: number | null;
}

export interface PolicyLevel {
  level: number;
  label: string;
  on: boolean;
  minutes: number | string;
  target: string;
}

export interface OncallSlot {
  id: number;
  weekday: number;
  day: string;
  start: string;
  end: string;
  start_minute: number;
  end_minute: number;
  name: string;
  surname: string;
  person: string;
  email: string;
  phone: string;
  phone2: string;
  enabled: boolean;
  window: string;
}

export interface OncallWeek {
  weekdays: string[];
  slots: OncallSlot[];
  now: OncallSlot | null;
  timezone: string;
  people: { name: string; surname: string; label: string; email: string; phone: string; phone2: string }[];
  can_edit: boolean;
}

export interface NoiseFilter {
  id: number;
  name: string;
  enabled: boolean;
  alertnames: string[];
  scope: "all" | "asset" | "type" | "site" | "service";
  scope_value: string;
  days: number[];
  start_minute: number | null;
  end_minute: number | null;
  max_value: number | null;
  action: "quiet" | "delay";
  delay_minutes: number;
  expires_at: string | null;
  owner: string;
  note: string;
  hits: number;
  last_hit_at: string | null;
  created_by: string;
}

export interface Automation {
  filters: NoiseFilter[];
  pipeline: { incidents_today: number; filtered_today: number; muted_today: number; routed_today: number; notifications_today: number };
  rules: {
    id: number;
    name: string;
    description: string;
    enabled: boolean;
    severity: string;
    condition: { alertname?: string; metric?: string; operator?: string; value?: number | string };
    playbook: string | null;
    playbook_id: number | null;
    policy: string | null;
    policy_id: number | null;
    hits_30d: number;
  }[];
  playbooks: { id: number; name: string; description: string; steps: string[]; rules: number }[];
  policies: { id: number; slug?: string; name: string; steps: PolicyStep[]; rules: number; levels?: PolicyLevel[] }[];
  maintenance: { asset: string; asset_id?: string; summary: string; starts_at: string | null; ends_at: string | null; active: boolean }[];
  can_edit: boolean;
  can_edit_maintenance?: boolean;
}

export interface Simulation {
  outcome: "incident" | "muted" | "filtered" | "delayed";
  filter?: string;
  trace: string[];
  rule: string | null;
  severity?: string;
  playbook: string | null;
  policy: string | null;
  steps: PolicyStep[];
}

export interface PeriodStats {
  incidents: number;
  critical: number;
  high: number;
  warning: number;
  mtta_min: number | null;
  mttr_min: number | null;
  reviewed: number;
}

export interface Reports {
  days: number;
  from: string;
  to: string;
  current: PeriodStats;
  previous: PeriodStats;
  daily: { day: string; critical: number; high: number; other: number }[];
  by_type: ({ group: string } & PeriodStats)[];
  noisy_assets: { asset: string; incidents: number }[];
  top_alerts: { alert: string; incidents: number }[];
  host_down: { asset: string; minutes: number }[];
  scheduled: ScheduledReport[];
  can_edit: boolean;
}

export interface ScheduleSpec {
  repeat: boolean;
  every_minutes: number;
  weekdays: number[];
  preset: string;
  cron?: string;
}

export interface ScheduledReport {
  id: number;
  name: string;
  kind: string;
  recipients: string[];
  schedule: string;
  spec?: ScheduleSpec;
  asset_ids?: string[];
  enabled: boolean;
  last_run_at: string | null;
  next_run_at: string | null;
}

export interface SettingRow {
  key: string;
  group: string;
  label: string;
  kind: "str" | "bool" | "int" | "enum" | "list" | "url";
  store: "yaml" | "secret" | "env";
  editable: boolean;
  help: string;
  test: string;
  choices?: string[];
  value?: unknown;
  effective?: unknown;
  set?: boolean;
}

export interface SettingsData {
  groups: Record<string, string>;
  settings: SettingRow[];
}

export interface Doctor {
  overall: string;
  components: Record<string, { status: string; label?: string; why?: string; detail?: string }>;
  failed: string[];
}

export interface KnownError {
  id: number;
  title: string;
  alertname: string;
  pattern: string;
  symptoms: string;
  workaround: string;
  fix: string;
  link: string;
  status: string;
  created_by: string;
  updated_at: string | null;
}
