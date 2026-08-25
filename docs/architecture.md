# ForgeSRE Architecture Proposal

**Status:** amended for V0.1 (see `docs/v0.1.md`)  
**Date:** 2026-08-20  
**Scope:** Long-term architecture. V0.1 runtime uses Python/FastAPI + Bash, not Go/React/Caddy.

This document remains the long-term contract (NetBox, discovery, HA, extra agents). **V0.1 implementation follows `docs/v0.1.md`.** If the two disagree on V0.1 scope or language, `docs/v0.1.md` wins.

---

## 1. What we are building

ForgeSRE is a small, self-hosted, offline-first operations layer for physical data-center infrastructure.

It does **not** replace Prometheus, Grafana, Loki, or NetBox. It connects them so a human can go from “a device exists” to “an incident is explained, assigned, and escalated” without assembling that chain by hand.

```
What exists?          Inventory (local and/or NetBox)
What is happening?    Prometheus + Loki
What happened?        ForgeSRE Incident
Why?                  AI RCA (optional, local, read-only)
How sure?             Evidence + confidence
Who owns it?          Asset + Playrule
What should we do?    Playbook
Whom to notify?       Escalation
Is ForgeSRE healthy?  Doctor
How do we install it? Setup Wizard
How do we run it?     CLI + backup + update + docs
```

V1 monitors physical servers, network devices, storage, disks, filesystems, NICs, hardware health, availability, and infrastructure logs. It does **not** do APM, Kubernetes, tracing, or business metrics.

---

## 2. Design principles

1. **Smallest system that solves the defined problem.** Every container must earn its place.
2. **Reuse proven software** for metrics, logs, inventory, and visualization.
3. **One operator-facing config file.** `.env` is deployment only. Secrets never live in YAML.
4. **AI is read-only.** Observe, investigate, explain, recommend. Never change infrastructure.
5. **Degraded > down.** Grafana, Loki, NetBox, and LLM failures must not stop monitoring or incident creation.
6. **Explainable operations.** Wizard, Doctor, and RCA must answer what / why / how to test / how to fix.
7. **Boring technology.** V0.1: Python/FastAPI + PostgreSQL + Jinja2 + Bash. Long-term ops CLI may still become a binary. No extra broker, no extra metrics database, no extra CMDB.

---

## 3. Architecture decisions

These are the choices that keep the service count down.

| ID | Decision | Why |
|---|---|---|
| ADR-1 | **One ForgeSRE process** serves HTTP API, embeds the UI, and runs workers | Avoids a separate UI container, a separate worker container, and a message broker |
| ADR-2 | **Python 3.12 + FastAPI for Core; Bash for install/doctor/backup** (V0.1) | Operator must be able to read and debug the code. Host still only needs Docker/Compose/Bash/Git; Python stays in the image |
| ADR-3 | **Jinja2 + small vanilla JS**, served by Core | Avoid a Node toolchain; pages stay simple and server-rendered |
| ADR-4 | **PostgreSQL is the only ForgeSRE datastore** | Users, assets cache, incidents, playrules, jobs, audit. Job queue is a Postgres `jobs` table; claim is pending-then-running (`.filter_by(status="pending").first()`), not `FOR UPDATE SKIP LOCKED` |
| ADR-5 | **No Redis for ForgeSRE** | Redis exists only if bundled NetBox is enabled, because NetBox requires it |
| ADR-6 | **Grafana Alloy is the unified collector** (longer-term) | Replaces Promtail; SNMP/blackbox unification is later. **V0.5 ships standalone `snmp_exporter`** on `:9116` |
| ADR-7 | **Caddy is the only published entrypoint** | One URL, TLS, routing to Core / Grafana / NetBox. Users do not manage eight ports |
| ADR-8 | **AI agents are in-process modules**, not microservices | Five named agents remain as code packages talking to one LLM HTTP API |
| ADR-9 | **Open WebUI is not in any default profile** | Optional later. It is not the ForgeSRE UI |
| ADR-10 | **No bundled Postfix by default** | External SMTP covers the common case. Optional Compose profile `mailbox` (docker-mailserver + Roundcube) is opt-in via `./forgesre mailbox`, not install. Internet receive still needs MX + port 25 |
| ADR-11 | **Compose profiles, one `docker-compose.yml`** | Not a pile of compose files. Wizard sets `COMPOSE_PROFILES` |
| ADR-12 | **Local inventory is a first-class SoT** | Minimal must work without NetBox. When NetBox is on, NetBox is SoT and ForgeSRE caches |
| ADR-13 | **Bundled LLM = OpenAI-compatible llama.cpp server** | Same client code for bundled and “existing local LLM” |
| ADR-14 | **REST `/api/v1`, not GraphQL** | Simple, cacheable, easy to audit |
| ADR-15 | **Alertmanager has one webhook receiver** | Grouping/wait stay in Alertmanager. Business routing stays in Playrules |
| ADR-16 | **Code Agent off in V1** | No sandbox runtime until the investigation path is proven |
| ADR-17 | **Discovery is the same Go module, separate container** | Privilege isolation (scan network) without giving Core extra capabilities |

---

## 4. What we are not adding (and why)

| Idea | Verdict | Reason |
|---|---|---|
| Custom metrics TSDB | Rejected | Prometheus already does this |
| Custom log TSDB | Rejected | Loki already does this |
| Custom DCIM/IPAM | Rejected | NetBox already does this |
| Promtail | Rejected | Alloy is the current collector |
| Separate UI container | Rejected | Embedded SPA is enough |
| Agent fleet (N containers) | Rejected | In-process agents + one LLM |
| Open WebUI in default install | Rejected for V1 | Extra UI, extra auth, extra failure domain |
| Bundled Postfix | Optional profile `mailbox`, off by default | Internet MX is still the operator’s problem (port 25, SPF/DKIM) |
| Redis for ForgeSRE jobs | Rejected | PostgreSQL queue is enough |
| Kafka / NATS | Rejected | No throughput justification |
| Kubernetes in V1 | Rejected | Out of product scope; Compose is the deployment |
| Cloud LLM as default | Rejected | Offline-first. External OpenAI-compatible URL may be configured, but must never be required |
| Auto-remediation | Rejected for V1 | Explicit future feature, isolated from AI |
| Netdisco / full discovery suite | Rejected for V1 | Too large. Thin CIDR/SNMP classifier + approval queue is enough |
| Grafana as the ForgeSRE UI | Rejected | Grafana stays “Open Grafana” for engineers |

If a component can be removed without losing a V1 capability, it is removed. The list above is that cut.

---

## 5. System architecture

```text
                         operators (browser / CLI)
                                   │
                                   ▼
                          ┌────────────────┐
                          │     Caddy      │  TLS, single URL
                          │   (ingress)    │
                          └───────┬────────┘
              ┌───────────────────┼───────────────────┐
              ▼                   ▼                   ▼
     Grafana (optional)   ForgeSRE Core        NetBox (optional)
     Loki/NetBox UI       API + UI + workers   DCIM/IPAM SoT
                                  │
         ┌────────────────────────┼────────────────────────┐
         ▼                        ▼                        ▼
   PostgreSQL              Prometheus +              Local LLM
   (ForgeSRE DB,           Alertmanager              (optional)
    optional NetBox DB)           │
                                  ▼
                           Grafana Alloy
                           logs + SNMP + probes
                                  │
                                  ▼
                                 Loki
                                  │
                                  ▼
                          Discovery worker
                          (scan → approval)
                                  │
                                  ▼
                            Infrastructure
                     servers, switches, storage
```

**ForgeSRE Core is the orchestration / business layer.** Everything else is either a specialized engine we reuse, or an optional integration.

### Runtime failure domains

| If this fails | What continues | What degrades |
|---|---|---|
| Caddy | Nothing through the URL (containers still run) | Operator access |
| Core | Prometheus still scrapes; Alertmanager still fires | No new ForgeSRE incidents, no UI |
| PostgreSQL | Metrics and logs | Core, incidents, auth |
| Prometheus / Alertmanager | Core, inventory, logs | New metric alerts |
| Loki / Alloy | Metrics and incidents | Log evidence, RCA quality |
| Grafana | Everything | Deep dashboards |
| NetBox | Local cache + monitoring | Inventory freshness, DCIM UI |
| LLM | Monitoring, alerting, incidents, playbooks | AI RCA |
| SMTP | Everything except outbound mail | Notifications |

AI is never on the critical path for monitoring.

---

## 6. Component catalog

For each component: why it exists, what it does, dependents, whether it can be removed, bundled/external, security, resources.

### 6.1 Caddy (ingress)

- **Why:** One hostname, TLS, and path routing. Without it the product is a pile of ports.
- **What:** Terminates TLS; reverse-proxies `/` to Core, `/grafana` to Grafana, `/netbox` to NetBox.
- **Depends on:** Core. Optionally Grafana, NetBox.
- **What depends on it:** Operators.
- **Removable?** Only if we accept multi-port UX. Not recommended.
- **Mode:** Always bundled. Not a user-facing “external Caddy” in V1 (can sit behind an existing LB later).
- **Security:** Least-privilege container; no Docker socket; TLS by default (`internal-ca` or operator certs).
- **Resources:** ~64–128 MB RAM.

### 6.2 ForgeSRE Core

- **Why:** This is the product. Inventory projection, HTTP SD, incident workflow, playrules, playbooks, escalation, AI orchestration, auth, audit, doctor, wizard backend.
- **What:** `forgesre serve` + in-process workers. Serves embedded UI.
- **Depends on:** PostgreSQL. Optionally Prometheus, Alertmanager, Loki, NetBox, LLM, SMTP.
- **What depends on it:** UI, CLI, Alertmanager webhook, Prometheus HTTP SD, Discovery worker.
- **Removable?** No.
- **Mode:** Always bundled (it *is* ForgeSRE).
- **Security:** No Docker socket. No host FS except configured data dir. No SSH. Read-only credentials to Prometheus/Loki/NetBox. LLM network allowlisted. Webhook authenticated.
- **Resources:** ~256–512 MB RAM baseline; more with concurrent investigations.

### 6.3 ForgeSRE CLI

- **Why:** Operators must not need `docker compose logs` for normal work.
- **What:** Same binary: `install`, `setup`, `status`, `doctor`, `update`, `backup`, `restore`, `logs`, `config`.
- **Depends on:** Core API and/or local compose project, depending on command.
- **Removable?** No (scripts can wrap it, but the CLI is the interface).
- **Mode:** Bundled with Core image; also shipped as host binary from `install.sh`.
- **Security:** Backup/restore need access to secrets and volumes; run as the install user, not as AI.
- **Resources:** Negligible.

### 6.4 PostgreSQL

- **Why:** Durable system of record for ForgeSRE. Also hosts NetBox database when NetBox is bundled (separate database, same instance).
- **What:** Relational store + job queue.
- **Depends on:** Disk in `FORGESRE_DATA`.
- **What depends on it:** Core, bundled NetBox.
- **Removable?** No for ForgeSRE. Do not run two Postgres instances unless NetBox is external and already has its own.
- **Mode:** Bundled for ForgeSRE. External Postgres is a later overlay, not V1 default (keeps install simple).
- **Security:** Password in `secrets/secrets.env`; not published outside the compose network; restricted container FS.
- **Resources:** ~256 MB+; disk grows with incidents/audit.

### 6.5 Prometheus

- **Why:** Metrics engine. We will not write one.
- **What:** Scrapes HTTP SD from Core, Alloy exporters, and node_exporter on hosts. Stores TSDB. Evaluates alert rules generated from monitoring profiles.
- **Depends on:** Core HTTP SD; Alloy and/or node_exporter; Alertmanager.
- **What depends on it:** Alertmanager, Grafana, Core (query for evidence), Engineer UI.
- **Removable?** No for any profile. Metrics are the product’s foundation.
- **Mode:** Bundled or external.
- **Security:** No write access to infrastructure. Admin API not exposed via Caddy in default.
- **Resources:** 1–4 GB RAM typical for a mid-size DC; retention configurable (`retention: 15d` default).

### 6.6 Alertmanager

- **Why:** Grouping, inhibition, wait, then one webhook to ForgeSRE.
- **What:** Receives Prometheus alerts; posts to Core `/api/v1/webhooks/alertmanager`.
- **Depends on:** Prometheus, Core webhook.
- **What depends on it:** Incident creation.
- **Removable?** Not if we use Prometheus alerts. Do not reimplement grouping in Core in V1.
- **Mode:** Bundled or follows Prometheus (if Prometheus is external, Alertmanager may also be external).
- **Security:** Webhook HMAC or shared token. No notification routing in AM beyond the ForgeSRE receiver (Playrules own business routing).
- **Resources:** ~64–128 MB.

### 6.7 Grafana Alloy

- **Why:** One collector instead of Promtail + snmp_exporter + blackbox_exporter (later).
- **What:** Tail/push logs to Loki; optional future SNMP/ICMP. **Today (V0.5): SNMP is `prom/snmp-exporter`, not Alloy.**
- **Depends on:** Loki (logs), Prometheus (scrape or remote-write), secrets for SNMPv3.
- **What depends on it:** Logging, network-device metrics, availability probes.
- **Removable?** Yes if logging and SNMP/probes are both disabled (Minimal). Required for Standard.
- **Mode:** Bundled or external.
- **Security:** SNMPv3 read-only, minimal scope. No write community. Credentials in secrets, never in `forgesre.yml`.
- **Resources:** ~128–512 MB depending on SNMP target count.

### 6.8 Loki

- **Why:** Local log store. Needed for evidence and RCA. Not needed to *detect* metric alerts.
- **What:** Log TSDB, filesystem backend (no object storage in V1).
- **Depends on:** Alloy (or compatible shipper), disk.
- **What depends on it:** Engineer UI, Data Agent, RCA quality, Grafana explore.
- **Removable?** Yes for Minimal. Standard default on.
- **Mode:** Bundled or external.
- **Security:** Not published publicly; Core queries with service credentials. Log content may contain secrets — redaction policy in later hardening.
- **Resources:** CPU light; disk heavy. Size by log volume.

### 6.9 Grafana

- **Why:** Detail visualization. ForgeSRE UI is summary + “Open Grafana”.
- **What:** Dashboards for Prometheus/Loki. SSO later; V1 can use a service account + link-out.
- **Depends on:** Prometheus, Loki.
- **What depends on it:** Engineer deep-dive, not the incident workflow.
- **Removable?** Yes for Minimal. Standard default on.
- **Mode:** Bundled or external.
- **Security:** Behind Caddy; no anonymous admin; separate Grafana password in secrets.
- **Resources:** ~128–256 MB.

### 6.10 Inventory: local provider

- **Why:** Asset identity must exist even without NetBox. IP is not identity; Asset ID is.
- **What:** First-class tables in PostgreSQL (assets, interfaces, IPs as attributes).
- **Depends on:** PostgreSQL, optional Discovery.
- **What depends on it:** HTTP SD, incidents, playrules, escalation, UI.
- **Removable?** No. Even with NetBox, Core keeps a cache/projection.
- **Mode:** Always present. SoT when `inventory.provider: local`.
- **Security:** Changes audited. RBAC on mutations.
- **Resources:** In Core/Postgres budget.

### 6.11 NetBox

- **Why:** Real DCIM/IPAM. Do not rebuild it.
- **What:** Source of truth when `inventory.provider: netbox`. Core syncs outbound (read) and writes only approved discovery (create device) via API.
- **Depends on:** PostgreSQL (shared instance if bundled), Redis (bundled only), Caddy.
- **What depends on it:** Inventory freshness, Engineer “Open NetBox”, Discovery approve path.
- **Removable?** Yes. Minimal uses local inventory. Standard *offers* NetBox; wizard may disable it.
- **Mode:** Bundled or external.
- **Security:** API token in secrets, least privilege. Core does not grant AI the NetBox token with write scope. Discovery writes go through Core, not the LLM.
- **Resources:** Heavy. Bundled adds NetBox + worker + Redis: roughly +1–2 GB RAM. Prefer **external** NetBox in production DCs that already have it.

**Wizard default:** Standard enables Loki/Alloy/Grafana. NetBox is a separate question, default **external if URL given**, else **local inventory**. Bundled NetBox is opt-in because it is the largest operational cost in Standard. This slightly relaxes “Standard always includes NetBox” in favor of operability; the capability remains.

### 6.12 Discovery worker

- **Why:** Prometheus must not require hand-maintained targets. Humans must approve unknown devices by default.
- **What:** Scans configured CIDRs (TCP connect / ICMP), optional SNMP `sysDescr`, classifies, opens an approval item. Does not invent a full DCIM.
- **Depends on:** Core API. Optional NetBox via Core.
- **What depends on it:** New asset proposals; HTTP SD after approval.
- **Removable?** Yes if inventory is fully manual. Default on for Standard, available in Minimal.
- **Mode:** Always ForgeSRE-bundled (small scanner). Not a third-party discovery product in V1.
- **Security:** Separate compose service; `cap_net_raw` only if ICMP is enabled; no Docker socket; no credentials to change devices. Results are proposals, not live inventory, unless mode=automatic (still audited).
- **Resources:** ~64–128 MB. Scan rate limited.

### 6.13 Local LLM

- **Why:** RCA without Internet. Optional.
- **What:** OpenAI-compatible HTTP server (bundled llama.cpp). Core sends prompts + retrieved evidence; model returns structured RCA JSON.
- **Depends on:** Model weights in the offline bundle or operator-provided path.
- **What depends on it:** AI agents / RCA / Visual investigation narrative.
- **Removable?** Yes. First-class `disabled`. Monitoring and incidents keep working.
- **Mode:** Bundled, external local endpoint, or disabled. Cloud is not a first-class provider in V1.
- **Security:** No tools that mutate infra. No Docker socket. No host mounts except the model file. Egress deny in offline mode. Outputs stored as evidence and audit.
- **Resources:** Dominant cost. CPU-only 7B–14B: plan **16–32 GB RAM**. GPU optional. Wizard must state this before enable.

### 6.14 Notification service (in Core)

- **Why:** Email/webhook after playbook/escalation. Not an “AI email agent”.
- **What:** Templates + SMTP + outbound webhooks. Retry via job queue.
- **Depends on:** External SMTP. Secrets for SMTP password.
- **What depends on it:** Escalation.
- **Removable?** Notifications can be disabled; incidents still exist.
- **Mode:** External SMTP only in V1.
- **Security:** No LLM-authored unrestricted email body without template; AI may fill *fields*, send path is deterministic. Recipients come from policies, not the model.
- **Resources:** In Core budget.

### 6.15 node_exporter (on targets, not a ForgeSRE service)

- **Why:** Linux hardware/OS metrics.
- **What:** Runs on servers, not in the ForgeSRE compose file.
- **Depends on:** Host install / existing exporters.
- **What depends on it:** `linux-*` monitoring profiles.
- **Removable?** Yes for network-only sites (SNMP instead).
- **Mode:** External by nature. Docs + profile tell Prometheus how to scrape via SD labels.
- **Security:** Listen on management network only; read-only.
- **Resources:** On each server, small.

### 6.16 Explicitly out of V1 runtime

Open WebUI, Promtail, blackbox_exporter as a separate container, Redis (unless bundled NetBox), extra agent containers, Code Agent sandbox VM. Postfix is **not** in the default stack.

**V0.5 exception:** `snmp_exporter` *is* a compose service (`127.0.0.1:9116`). Alloy SNMP remains a later unification.

**V0.7 exception:** optional Compose profile `mailbox` (docker-mailserver + Roundcube) via `./forgesre mailbox`. Off at install.

---

## 7. Container list by profile

One compose file. Profiles stack: `minimal`, `standard`, `ai`, plus optional `netbox`, `mailbox` (on-box mail + Roundcube).

### Minimal — `COMPOSE_PROFILES=minimal`

| Container | Required |
|---|---|
| caddy | yes |
| forgesre-core | yes |
| postgres | yes |
| prometheus | yes |
| alertmanager | yes |
| forgesre-discovery | optional (wizard) |

**Intent:** metrics + alerts + incidents + auth + local inventory. No logs, no Grafana, no AI.

**Host sizing (starting point):** 2 vCPU, 8 GB RAM, 100 GB disk.

### Standard — `minimal,standard`

Adds:

| Container | Required |
|---|---|
| alloy | yes |
| loki | yes |
| grafana | yes |
| forgesre-discovery | yes unless disabled |

Optional overlay `netbox`:

| Container | Required when bundled NetBox |
|---|---|
| netbox | yes |
| netbox-worker | yes |
| redis | yes (NetBox only) |

**Host sizing without bundled NetBox:** 4 vCPU, 16 GB RAM, 250 GB+ disk (logs).  
**With bundled NetBox:** add ~2 GB RAM.

### Full AI — `minimal,standard,ai`

Adds:

| Container | Required |
|---|---|
| llm (llama.cpp) | yes unless `ai.llm.mode=external` |

**Host sizing:** 8+ vCPU, 32 GB RAM typical for a small local model; GPU optional.

External mode for Prometheus/Loki/Grafana/NetBox/LLM **omits** that container and points `url:` at the existing service. ForgeSRE must not assume bundled topology.

### Mailbox — optional `mailbox` (off by default)

Not part of install. `./forgesre mailbox` adds:

| Container | Role |
|---|---|
| mailserver (docker-mailserver) | Postfix + Dovecot. Inbound :25, submission 127.0.0.1:587, IMAP :993 |
| roundcube | Webmail client on :8081 |

Core stays on host networking. SMTP (Gmail / Outlook) is independent of this profile unless `--bind-core`. ForgeSRE has no IMAP UI.

---

## 8. Repository structure

Proposed structure (implementation fills this in later phases). Fewer top-level folders than a polyrepo of services.

```text
forge-sre/
├── README.md
├── LICENSE
├── SECURITY.md
├── CONTRIBUTING.md
├── CHANGELOG.md
├── .gitignore
├── .env.example
├── docker-compose.yml          # single file, compose profiles
├── cmd/
│   └── forgesre/               # main: CLI + serve
├── internal/                   # Go business logic (not importable)
│   ├── api/
│   ├── auth/
│   ├── config/
│   ├── inventory/
│   ├── discovery/
│   ├── monitoring/             # HTTP SD, profile → scrape/alerts
│   ├── incident/
│   ├── playrule/
│   ├── playbook/
│   ├── escalation/
│   ├── notify/
│   ├── ai/                     # agents as packages
│   ├── doctor/
│   ├── backup/
│   └── audit/
├── web/                        # React UI
├── config/
│   ├── forgesre.example.yml
│   ├── examples/               # playrule / playbook / escalation samples
│   └── monitoring-profiles/    # linux-standard, network-switch, ...
├── secrets/                    # .gitkeep only
├── docs/                       # product docs + this architecture
├── scripts/                    # install.sh thin wrapper → forgesre
├── tests/
│   ├── integration/
│   └── e2e/
└── testdata/                   # small fixtures
```

**Why this and not `backend/` + `frontend/` + `agents/` + `deployment/`:**

- `cmd/` + `internal/` is idiomatic Go and keeps agents from becoming a fake distributed system.
- `scripts/` stay thin so logic lives in the binary (testable).
- No `deployment/` split in V1: Compose *is* deployment.

---

## 9. Data model

PostgreSQL. UUID primary keys unless noted. Asset ID is the identity; IPs/hostnames are attributes.

```text
users
  id, email, name, password_hash, role, is_active, created_at, last_login_at

sessions
  id, user_id, token_hash, expires_at, created_at, ip, user_agent

assets
  id (asset_id), hostname, site, rack, vendor, model, serial,
  role, customer, owner, contact_name, owner_email, owner_phone, notes,
  monitoring_profile, playbook, sla,
  escalation_policy, lifecycle (proposed|active|ignored|decommissioned),
  source (manual|discovery|netbox), netbox_id nullable, created_at, updated_at

asset_ips
  asset_id, ip, version, is_mgmt, unique(ip) not required globally
  -- IP is not unique identity; duplicates possible across VRFs later

asset_macs
  asset_id, mac, interface

asset_interfaces
  asset_id, name, mac, vlan, speed

discovery_candidates
  id, ip, mac, fingerprint, proposed_role, status (new|approved|ignored),
  raw, seen_at, decided_by, decided_at

monitoring_profiles
  name, scrape_ports, exporters, alert_rule_bundle, labels

playrules
  id, name, yaml, scope_type, scope_id, enabled, version, updated_by, updated_at

playbooks
  id, name, yaml, version, updated_by, updated_at

escalation_policies
  id, name, yaml, version

incidents
  id (human INC-n), severity, status,
  started_at, ended_at, customer, owner,
  playrule_id, playbook_id, escalation_policy_id,
  ai_confidence, ack_by, ack_at, created_at

incident_assets
  incident_id, asset_id

incident_evidence
  id, incident_id, kind (metric|log|alert|netbox|ai|note),
  ref, payload jsonb, captured_at

incident_rca
  incident_id, summary, causes jsonb, confidence, model, prompt_hash,
  created_at

incident_events          -- audit trail of the incident
  id, incident_id, at, actor (user|system|ai), kind, data jsonb

jobs
  id, kind, run_at, locked_at, attempts, payload jsonb, last_error

notifications
  id, incident_id, channel, target, status, sent_at, error

audit_log
  id, at, actor_user_id, actor_type, action, object_type, object_id,
  ip, data jsonb

ai_runs
  id, incident_id, agent, started_at, ended_at, input_refs, output jsonb,
  model, error
```

**Roles** are an enum on `users.role` in V1 (not a general ACL engine):

`super_admin | admin | engineer | analyst | viewer`

**Incident status:** `open | acknowledged | investigating | resolved | closed`

Indexes: incidents by status/started_at; assets by hostname/serial/netbox_id; jobs by `run_at` where unlocked; audit by at/actor.

NetBox remains authoritative for DCIM fields when provider=netbox. ForgeSRE may store denormalized copies; sync is one-way read plus explicit approve-create.

---

## 10. API boundaries

Base: `https://$FORGESRE_DOMAIN/api/v1`  
Auth: session cookie for UI; bearer token for CLI/automation.

### Public / operator API (RBAC enforced)

| Area | Methods |
|---|---|
| `/health` `/ready` | unauthenticated liveness/readiness |
| `/auth/login` `/auth/logout` `/auth/me` | local auth |
| `/assets` | CRUD (role-gated); list/filter |
| `/discovery/candidates` | list, approve, ignore |
| `/incidents` | list, get, ack, comment, resolve |
| `/incidents/:id/evidence` | get |
| `/incidents/:id/rca` | get; `POST .../investigate` starts AI job |
| `/playrules` `/playbooks` `/escalation-policies` | CRUD (admin) |
| `/users` | admin |
| `/system/status` `/system/doctor` | status; doctor may be admin-only for fixes |
| `/system/config` | redacted config view |

### Integration API (service tokens, not user roles)

| Endpoint | Caller | Purpose |
|---|---|---|
| `POST /webhooks/alertmanager` | Alertmanager | Create/update incidents |
| `GET /sd/prometheus` | Prometheus | HTTP service discovery |
| `POST /internal/discovery/results` | Discovery worker | Upsert candidates |

### Outbound (Core is client)

- Prometheus HTTP API (query)
- Loki HTTP API (query)
- NetBox REST (read; write only on approve)
- LLM `/v1/chat/completions`
- SMTP
- Optional notification webhooks
- Grafana URL is link-out, not required as an API in V1

**No GraphQL. No inbound SSH. No Docker API.**

UI is static files from Core (`/` except `/api`, `/grafana`, `/netbox`).

---

## 11. Authentication and RBAC

V1: **local users only** (email + password, Argon2id). Password reset is an admin-set password. OIDC/LDAP later.

Sessions: HTTP-only secure cookies. CLI uses API tokens stored in `~/.config/forgesre` or `FORGESRE_TOKEN`.

**Current product (V0.4):** Analyst can add/edit assets and write playrules/playbooks. Engineer has PromQL/evidence. Admin has users and demos. Super admin is the install user. See [`operator-handbook.md`](operator-handbook.md) §3.

| Role | Can |
|---|---|
| Viewer | Read incidents/assets summaries |
| Analyst | Viewer + ack, inventory add/edit, RCA (analyst view), playrules/playbooks; **no** admin, **no** PromQL, **no** user admin |
| Engineer | Analyst + raw evidence, PromQL/log queries via Core proxy or deep links, asset technical fields |
| Admin | Engineer + users (except super admin), integrations, system config, Run demo panel |
| Super Admin | Admin + initial bootstrap, recovery, destructive config |

Analysts never get administrative privileges. Engineers are not admins by default.

Every login, logout, config/asset/playrule/playbook/user change, incident state change, escalation, notification, and AI run is written to `audit_log` / `ai_runs`.

---

## 12. Discovery flow

Default mode: **semi-automatic**.

```text
CIDRs in forgesre.yml
        │
        ▼
Discovery worker (periodic)
        │  fingerprint: ip, open ports, optional SNMP sysDescr
        ▼
POST /internal/discovery/results
        │
        ▼
discovery_candidates (status=new)
        │
        ▼
UI: NEW DEVICE DETECTED / Approve / Ignore
        │
        ├─ Ignore → status=ignored
        └─ Approve → create asset (local) and/or NetBox device
                     lifecycle=active
                     monitoring_profile guessed, operator can override
                            │
                            ▼
                   HTTP SD updates on next poll
                            │
                            ▼
                   Prometheus scrapes new target
```

Manual mode: no scanner; assets entered in UI or imported.

Automatic mode: approve is skipped; still **audited**. Wizard must warn.

Prometheus configuration (scrape_configs) stays **stable**. Targets change only through HTTP SD. Adding a server, removing a server, changing IP/VLAN/hostname does not require editing Prometheus YAML.

---

## 13. Monitoring flow

```text
Monitoring profile (linux-standard, linux-storage, network-switch, storage-array)
        │
        ▼
Core renders:
  - SD labels (job, role, __address__, exporter)
  - Prometheus rule groups (from profile, not per-host)
        │
        ▼
Prometheus scrape  →  TSDB
        │
        ▼
Rule fires  →  Alertmanager  →  webhook  →  Core incident
```

**Profiles, not per-server config.** A host gets one `monitoring_profile`. Changing profile changes what is scraped and which rules apply, via labels.

Linux metrics: node_exporter on the host (operator-installed) or equivalent.  
Network: SNMPv3 via Alloy, read-only.  
Availability: Alloy probes (ICMP/HTTP) from the ForgeSRE network.

Core may proxy Prometheus/Loki queries for Engineer UI so the browser does not talk to those backends directly.

---

## 14. Incident flow

```text
Prometheus
    → Alertmanager (group / wait)
    → POST /webhooks/alertmanager
    → Core: dedupe by fingerprint (alertname + asset + fingerprint labels)
    → Incident open
    → Match Playrule (most specific scope wins: asset > group > site > customer > global)
    → Attach Playbook + Escalation policy
    → Enqueue notify (0m step)
    → If ai.enabled: enqueue investigation job
    → Data Agent gathers evidence (Prom/Loki/inventory) into incident_evidence
    → Analysis Agent produces RCA JSON (causes, confidence, why)
    → Report Agent writes analyst summary
    → Visual Agent stores a timeline of investigation steps (data, not animation)
    → Escalation ticker: if not acked, next step
    → Engineer/Analyst ack / resolve
```

If LLM is down, incident creation and playbook/escalation still run. RCA fields stay empty with `ai_status=degraded`.

Dedup: firing/resolved from Alertmanager update the same incident until resolved/closed.

---

## 15. AI agent architecture

Agents are **Go packages** in `internal/ai`, orchestrated by the SRE Agent. They share one LLM client. They have no OS on the infrastructure.

```text
SRE Agent (orchestrator, no extra model calls required beyond planning)
    ├── Data Agent      read Prometheus, Loki, inventory, prior incidents
    ├── Analysis Agent  correlate, classify, RCA hypotheses
    ├── Report Agent    analyst-facing summary
    └── Visual Agent    persist evidence chain / timeline for UI
```

Code Agent (optional, **off in V1**): would run in a sandbox on **already fetched** read-only data. Never `ssh`, `rm`, `mkfs`, `fdisk`, `systemctl`.

**Hard constraints**

- No SSH, no Docker socket, no kube credentials, no write NetBox token, no SMTP raw send except through Notification service templates.
- Tools are an allowlist: `prom_query`, `loki_query`, `get_asset`, `get_incident`, `search_incidents`.
- Model output is schema-validated JSON. If invalid, store error, do not page from free text.
- Prompt and retrieved evidence are stored for explainability (`ai_runs`).
- RCA results use timeline, candidates, evidence, and a ForgeSRE confidence score.

**Persona rendering** (same incident):

- Analyst: what / impact / severity / likely cause / confidence / owner / playbook / next escalation / visual timeline.
- Engineer: filesystem, growth, IOPS, PromQL, Loki, NetBox, Grafana, raw evidence, alternative hypotheses.
- Admin: not incident-first; config, inventory, health, audit.

---

## 16. Playrule schema

Deterministic. Versioned YAML stored in DB (and optionally files under `config/` for gitops later). **AI cannot mutate playrules.**

See `config/examples/playrule-certificate-expiry.yml`.

Logical model:

```yaml
apiVersion: forgesre.io/v1
kind: Playrule
metadata:
  name: string
  scope: { type: global|customer|site|group|asset, id?: string }
spec:
  enabled: bool
  severity: info|warning|high|critical
  when: { all?: [predicate], any?: [predicate] }
  match: { asset_roles?: [string], monitoring_profiles?: [string] }
  responsibility: { type: customer|team|owner|oncall, id?: string }
  playbook: string
  escalation_policy: string
```

Predicates operate on alert labels/annotations and asset fields (`certificate_expiry_days < 30` is expressed as alert labels produced by the monitoring profile, not as a new language in V1). Keep the expression language **small**: `eq`, `neq`, `lt`, `lte`, `gt`, `gte`, `contains`, `exists`.

---

## 17. Playbook and escalation schemas

Playbook V1 = **guidance + workflow + escalation hooks**. Not a remediation engine.

See `config/examples/playbook-disk-full-high.yml` and `config/examples/escalation-default-high.yml`.

Step `actor`: `analyst | engineer | system`  
System actions V1: `notify | wait_ack | escalate`  
No `run_command`, no `ssh`, no `patch`.

Escalation is a **separate object** from the playbook so one playbook can reuse policies. Channels V1: `email`, `webhook`. Later: SMS, Slack, Teams, Discord, tickets.

---

## 18. Configuration model

**Three files, three jobs.**

| File | Contains | Does not contain |
|---|---|---|
| `.env` | Version, domain, data dir, timezone, compose profiles, bind/port | Passwords, playrules, URLs of every subcomponent unless needed by compose |
| `config/forgesre.yml` | All feature/mode/url/enable flags | Secrets |
| `secrets/secrets.env` | Passwords, tokens, SNMP, SMTP, LLM key if any | Structural config |

Canonical example: `config/forgesre.example.yml`.

Validation: on `forgesre setup` / `doctor` / process start. Unknown keys are errors (fail closed), not silently ignored.

Generated files (Prometheus scrape, Alloy, Caddy, Alertmanager receiver) are **rendered into `$FORGESRE_DATA/generated/`** from `forgesre.yml`. Operators do not edit those by hand. That is how we avoid “dozens of unrelated config files” while still feeding existing software.

---

## 19. Secrets model

- Path: `secrets/secrets.env` (install sets mode `0600`, directory `0700`).
- Not committed. `secrets/.gitkeep` only.
- Never hardcoded. Never in `forgesre.yml`. Never in incident UI unless redacted.
- Compose `env_file` for services that need them. Core reads secrets and does not expose them on `/system/config`.
- SNMP and NetBox tokens used by Core/Alloy are read-only in documentation and in the tokens we generate when bundled.
- V1 backend = files. Vault/SOPS is a later backend with the same key names.

Minimum keys:

```text
POSTGRES_PASSWORD
FORGESRE_ADMIN_PASSWORD          # bootstrap, then users live in DB
ALERTMANAGER_WEBHOOK_TOKEN
GRAFANA_ADMIN_PASSWORD           # if Grafana enabled
NETBOX_API_TOKEN                 # if NetBox enabled
SMTP_USERNAME
SMTP_PASSWORD
LLM_API_KEY                      # optional even for local
SNMP_V3_USER
SNMP_V3_AUTH_PASSPHRASE
SNMP_V3_PRIV_PASSPHRASE
```

---

## 20. Installation wizard

`./install.sh` → `forgesre install` (or `forgesre setup` for reconfigure).

This is a **product feature**, not a README. Linear prompts (SSH-safe). Each question shows:

What is this? Why do I need it? Required? What if I skip it? Resources? Network? How to change later? How to verify?

### Steps

1. **Preflight** — Docker, Compose, CPU, RAM, disk, FS, permissions, ports, DNS, clock, offline flag  
2. **Profile** — Minimal / Standard / Full AI  
3. **Storage** — `FORGESRE_DATA`  
4. **Network** — domain, bind, ports, TLS mode  
5. **Security** — admin account, cookie/TLS, secret file creation  
6. **Inventory** — local vs NetBox bundled/external  
7. **Discovery** — off / semi / auto, CIDRs  
8. **Monitoring** — Prometheus bundled/external  
9. **Logging** — Loki/Alloy on/off  
10. **Grafana** — on/off, bundled/external  
11. **LLM** — bundled / existing local URL / disabled, with RAM warning  
12. **AI agents** — on only if LLM on  
13. **SMTP** — off or external host/port  
14. **Auth** — local admin  
15. **Validation** — config schema + doctor dry-run  
16. **Deploy** — compose up, migrate, health  
17. **Report** — write `installation-report.md`

Preflight failure example:

```text
✗ Port 443 is already in use.
Why: another process is bound to 443.
Test: ss -lntp | grep :443
Fix: 1) change FORGESRE_HTTP_PORT  2) stop the other service
```

Offline: `forgesre install --offline` never pulls images; loads from the bundle. Wizard still runs.

Reconfigure: `forgesre setup` rewrites `forgesre.yml` and regenerates `$FORGESRE_DATA/generated/`, then doctor.

---

## 21. Doctor / diagnostics

`forgesre doctor` is a primary tool. Every check returns:

**WHAT is wrong? WHY? HOW do I test? HOW do I fix?** Plus a doc link (`docs/...` or in-wizard Explain).

Checks (skip disabled components):

- compose ps / container health
- postgres connect + migrations
- volume permissions and disk %
- published ports vs config
- Prometheus `/-/ready` and SD reachable
- Alertmanager ready + webhook test (does not need a real outage)
- Loki/Alloy/Grafana ready when enabled
- NetBox API token
- Discovery last-success timestamp
- LLM `/v1/models` when enabled
- SMTP banner/`nc` equivalent from Core
- TLS files if mode=files
- config consistency (e.g. `ai.enabled` but `llm.mode=disabled`)

Output: human text default; `--json` for CI.

Not allowed as the only message: `connection refused`.

---

## 22. Backup, restore, upgrade

`forgesre backup` produces a single archive under `$FORGESRE_DATA/backups/`:

- PostgreSQL dump (ForgeSRE DB; NetBox DB if bundled)
- `config/forgesre.yml`
- generated config (optional, can be reproduced)
- playrules/playbooks/escalation (already in DB, dump covers them)
- secrets **file copy into a restricted tar member** with warning; or omit secrets and require them present on restore (wizard asks). Default: include secrets in the archive, mode 0600, documented.
- incident data (in DB)
- AI config (in yaml + db)
- **Not** full Prometheus/Loki TSDB by default (size). Flag `--include-metrics` / `--include-logs` for sites that want it.

`forgesre restore ARCHIVE` prints the plan and exits 1. `forgesre restore ARCHIVE --yes` stops Core when Docker is available, restores the DB + files, and tells you to run `./forgesre update`. It will not apply without `--yes`.

`forgesre update`:

1. Require recent backup or create one  
2. Check version compatibility  
3. Load images (offline bundle or pull if online)  
4. migrate DB  
5. rolling/compose recreate  
6. doctor  
7. print result  

No silent image `:latest` upgrades without a version pin in `.env`.

---

## 23. Offline deployment

```text
online build machine
    → forgesre-offline-bundle-VERSION.tar
         docker images (profile-selected)
         compose + binary + config examples + docs
         checksums (SHA256SUMS)
    → physical/media to air-gapped DC
    → ./install.sh --offline
```

`--offline` sets `system.mode=offline`, disables any update check, and fails if a component would need egress (except RFC1918 targets). LLM weights are a **separate** artifact (large); the bundle either includes a chosen model or documents copying it into `$FORGESRE_DATA/models`.

---

## 24. Security / threat model

### Assets we protect

Operator credentials, SNMP/NetBox tokens, infrastructure maps, logs (may contain secrets), incident data, the ability to page humans.

### Explicit non-goals for the AI subsystem

The model is not an automation engine. Future remediation must be a separate, off-by-default, audited subsystem.

### Threats and mitigations

| Threat | Mitigation |
|---|---|
| Stolen SMTP/NetBox/SNMP secrets | File secrets 0600; not in git; not in YAML; not shown in UI |
| Alertmanager webhook spoofing | Shared token/HMAC; compose network |
| Prompt injection via logs | Tool allowlist; schema-validated output; no infra tools; treat untrusted logs as data |
| AI used to change infra | No credentials that can change infra; no Docker socket; no SSH |
| SSRF via discovery/LLM tools | Allowlisted query APIs; CIDR allowlists for scan; no arbitrary URL fetch tool |
| Privilege confusion in UI | RBAC as table above; analyst ≠ admin |
| Container breakout | Drop caps; read-only root FS where possible; no privileged Core; Discovery only extra cap if ICMP |
| Supply chain | Pinned image digests in released compose; checksums on offline bundle |
| Log/PII leak to cloud LLM | Offline default; cloud provider not first-class; doctor warns if LLM URL is public |
| Session theft | Secure cookies; TLS default |
| Weak bundled defaults | Wizard generates passwords; no published default admin/admin in docs beyond first-run |

Trust boundary: **management network**. ForgeSRE is not a public SaaS. Caddy does not expose Prometheus/Alertmanager admin UIs by default.

---

## 25. Testing strategy

### Unit

Playrule matching, playbook step machine, config validation, incident dedup, RBAC, webhook fingerprinting, wizard rendering of generated config. No Docker required.

### Integration

Testcontainers or compose on CI: PostgreSQL, a stub Prometheus, stub Loki, stub NetBox, stub SMTP, stub LLM. Doctor should pass against stubs.

### End-to-end (one golden path)

```text
seed asset (linux-standard)
  → HTTP SD lists it
  → synthetic alert (or unit-level AM payload)
  → incident created
  → playrule matched
  → playbook attached
  → notification job recorded
  → if AI profile: stub LLM returns RCA JSON
  → analyst payload contains required fields
```

### RCA

`tests/test_v03.py` covers collector, anomalies, scoring, and ForgeRCA output shape. CI does not require a live LLM.

### What we do not test in V1 CI

Full NetBox UI, real SNMP hardware, GPU LLM quality (separate eval job).

---

## 26. Phased implementation plan

Do not implement all phases at once. Each phase must stay installable.

| Phase | Deliver | Exit criteria |
|---|---|---|
| **0** | This architecture, threat model, data model, config contract | Review sign-off |
| **1** | Core + Postgres + embedded UI + local auth + RBAC + audit | Login, users, empty asset list |
| **2** | Asset model + Prometheus + Alertmanager + HTTP SD + monitoring profiles + webhook → incident shell | Alert becomes an incident |
| **3** | Discovery worker + candidate UI; NetBox provider optional | Approve device → scrape target |
| **4** | Loki + Alloy + Grafana link-out | Logs on engineer view |
| **5** | Full incident model, evidence, ack, timeline | Analyst/Engineer UX without AI |
| **6** | Playrules, playbooks, escalation, email/webhook | Deterministic workflow |
| **7** | Local LLM client + in-process agents (read-only tools) | RCA optional |
| **8** | RCA schema + Visual timeline + persona views | Spec screens INC-1042 |
| **9** | Wizard, doctor, backup, restore, update, installation-report | Operable without raw docker |
| **10** | Offline bundle, image pins, hardening, full docs | Air-gap install documented |

Phase 9 is listed late in the original spec; **doctor and config validation should be stubbed from Phase 1** so we do not bolt operations on at the end. The full wizard UX still lands in Phase 9.

---

## 27. Risks and open points

1. **Bundled NetBox size** — mitigated by making bundled NetBox opt-in and recommending external NetBox.
2. **Alloy replacing snmp_exporter/blackbox** — V0.5 ships standalone snmp_exporter because operators need IF-MIB walks now. Alloy SNMP remains a later merge, not a blocker.
3. **Core availability** — V1 is a single Core replica. PostgreSQL is the durability story. HA is post-V1.
4. **Prometheus down vs Core down** — if Core is down, HTTP SD goes stale (Prometheus keeps last targets). Document it.
5. **LLM quality on infra RCA** — V1 ships the pipeline and schema; quality is eval, not a blocker for monitoring.
6. **License** — not chosen in this proposal. Recommend Apache-2.0 unless there is a reason otherwise.
7. **Grafana auth** — V1 may use a shared viewer link + separate Grafana login. Unified SSO is post-V1.

---

## 28. Mapping to the master spec

The proposal implements the requested first deliverable: diagrams, component list, containers, repo layout, data model, API, RBAC, discovery/monitoring/incident/AI flows, playrule/playbook schemas, config/secrets, wizard, doctor, backup, offline, threat model, tests, phases.

Intentional simplifications vs a naive reading of the spec:

- No Open WebUI, no Postfix in the default stack (optional `mailbox` profile is opt-in), no agent microservices, no Promtail, no Redis for ForgeSRE, no separate UI container.
- Alloy consolidates collectors.
- Local inventory so Minimal does not require NetBox.
- Doctor/backup logic lives in the Go CLI, with `scripts/*.sh` as wrappers.

These cuts exist to satisfy the higher-priority rule: **the smallest reliable system**, easy for the person who has to run it.
