# ForgeSRE operator handbook

This is the **day-2 guide for the whole system**: users, servers, monitoring, playrules, playbooks, incidents, and RCA.

Install and file-level config stay in [`install-config.md`](install-config.md). Version notes (`v0.1.md`, `v0.2.md`, `v0.3.md`) explain *what shipped*. This document explains *how you operate it*.

Code: https://github.com/nsimic2022/forgesre (`main`). Open the UI at `http://<VM-IP>:8080`.

ForgeSRE does **not** replace Prometheus, Grafana, Loki, or NetBox. It sits on top of them. AI is **read-only**: it never SSH-es, never runs playbooks, never writes NetBox.

---

1. [How the system fits together](#1-how-the-system-fits-together)
2. [Where work happens](#2-where-work-happens)
3. [Roles and who can click what](#3-roles-and-who-can-click-what)
4. [Screen map](#4-screen-map)
5. [Users and admins](#5-users-and-admins)
6. [Adding servers (inventory)](#6-adding-servers-inventory)
7. [Making a server actually monitored](#7-making-a-server-actually-monitored)
8. [Alerts become incidents](#8-alerts-become-incidents)
9. [Playrules](#9-playrules)
10. [Playbooks](#10-playbooks)
11. [Escalation and email](#11-escalation-and-email)
12. [Incident workflow](#12-incident-workflow)
13. [AI investigation (ForgeRCA)](#13-ai-investigation-forgerca)
14. [Worked example: onboard a Linux server](#14-worked-example-onboard-a-linux-server)
15. [Worked example: new alert + playrule + playbook](#15-worked-example-new-alert--playrule--playbook)
16. [CLI, API, and files on disk](#16-cli-api-and-files-on-disk)
17. [What V0.3 does not do yet](#17-what-v03-does-not-do-yet)

---

## 1. How the system fits together

```
Discovery / manual form / NetBox
        ↓
   Assets (inventory)
        ↓  scrape_address → Prometheus HTTP SD
   Metrics + alert rules
        ↓  Alertmanager webhook
   Incident
        ↓  match playrule by alertname
   Playbook (guidance) + escalation email
        ↓  Run AI investigation
   ForgeRCA (facts / hypotheses / evidence)
```

Seeded on first start:

| Object | What it is |
|---|---|
| User `FORGESRE_ADMIN_EMAIL` | `super_admin` from `secrets/secrets.env` |
| Asset `forge-demo-01` | Demo host `10.10.10.20` (not a real machine) |
| Playbooks `CPU-HIGH`, `DISK-FULL` | Guidance steps only |
| Playrules `high-cpu`, `high-disk` | Match Prometheus alerts `HighCPU` / `FilesystemUsageHigh` |
| Escalation `Default warning` | 0 / 15 / 30 minutes → generated email |
| Discovery candidate `10.20.30.41` | Demo row on `/discovery` so you can click Approve |

Lab demos (`./forgesre demo` and `./forgesre demo-rca`) fire **demo gauges on Core**, not real disk/CPU on a customer VM.

---

## 2. Where work happens

Three places. Do not mix them.

| Place | You use it for | Lives in |
|---|---|---|
| **UI** (`:8080`) | Users, assets, discovery Approve/Ignore, playrules, playbooks, incident status, RCA | PostgreSQL |
| **`config/forgesre.yml`** | Discovery CIDRs, NetBox URL, AI/LLM, SMTP on/off, Loki/Grafana | File on the VM |
| **Repo / generated files** | Prometheus *alert expressions*, scrape jobs, Alertmanager webhook | `monitoring/alerts.yml`, `.env`, `secrets/secrets.env` |

YAML under `config/examples/` is the **future spec** (Playrule/Playbook/Escalation as files). V0.3 does **not** import those files. Live playrules and playbooks are created in the UI (or API) and stored in Postgres.

After editing `config/forgesre.yml`, recreate Core:

```bash
docker compose up -d --force-recreate core
```

After editing `monitoring/alerts.yml`:

```bash
curl -fsS -X POST http://127.0.0.1:9090/-/reload
```

---

## 3. Roles and who can click what

| Role | Typical job | Can do |
|---|---|---|
| `viewer` | Read-only | Dashboard, assets, incidents |
| `analyst` | Triage | Above + Acknowledge, open AI, read playrules/playbooks |
| `engineer` | Inventory + incidents | Above + Approve/Ignore discovery, add assets, change incident status, see PromQL/LogQL |
| `admin` | Operate the product | Above + create users, playrules, playbooks, run demos, NetBox sync |
| `super_admin` | Install owner | Same as admin. Created only by `./install.sh` from secrets |

The UI **Create user** form cannot make another `super_admin`. Extra operators should be `admin`.

Login session lasts **12 hours** (httponly cookie).

---

## 4. Screen map

| Menu | URL | What you do there |
|---|---|---|
| Dashboard | `/` | Counts, doctor lights, pending discovery banner, demo buttons (admin) |
| Assets | `/assets` | List inventory. **Add asset** form (engineer+) |
| Asset detail | `/assets/<id>` | Hostname, IP, scrape address, related incidents |
| Discovery | `/discovery` | Scan, Approve / Ignore, optional NetBox sync |
| Incidents | `/incidents` | All incidents from Alertmanager |
| Incident | `/incidents/INC-…` | Ack / Resolve / Close, run RCA, playbook name |
| AI Investigation | `/ai/INC-…` | Facts, anomalies, hypotheses, evidence IDs |
| Playrules | `/playrules` | List, toggle, create (admin) |
| Playbooks | `/playbooks` | List steps, create (admin) |
| Escalation | `/escalation` | Seeded policy + generated notification log |
| System Health | `/health-ui` | Same checks as `./doctor.sh` |
| Administration | `/admin` | Create users, audit log (admin) |
| Grafana | `:3000` | Deep dashboards (separate login) |

---

## 5. Users and admins

### First login

Credentials are in `installation-report.md` and `secrets/secrets.env`:

- `FORGESRE_ADMIN_EMAIL` (default `admin@forgesre.local`)
- `FORGESRE_ADMIN_PASSWORD`

That account is `super_admin`. Do **not** re-run `./install.sh` on a live box unless you intend to regenerate passwords.

### Add an admin (or any user)

1. Sign in as admin / super_admin.
2. Open **Administration** (`/admin`).
3. Fill **Create user**: email, name, password, role (`admin` / `engineer` / `analyst` / `viewer`).
4. **Create**. The new user signs in at `/login`.

Same action via API (session cookie after UI login, or as the installer does):

```bash
curl -fsS -b cookies.txt -c cookies.txt -X POST http://127.0.0.1:8080/login \
  -d "email=admin@forgesre.local&password=YOUR_PASSWORD"

curl -fsS -b cookies.txt -X POST http://127.0.0.1:8080/api/v1/users \
  -H 'Content-Type: application/json' \
  -d '{"email":"ops@dc.local","name":"Ops Admin","password":"change-me","role":"admin"}'
```

There is **no edit/disable form** in V0.3. To rotate a password, create a new user or change the hash in Postgres. Audit rows for `user.create` and `login` show on `/admin`.

---

## 6. Adding servers (inventory)

A row on **Assets** is what ForgeSRE calls a server (or switch, or appliance). You can add one in three ways. Pick one path per host so you do not duplicate IPs.

### A. Manual (you already know hostname + IP)

Who: `engineer` or `admin`.

1. **Assets** → **Add asset**.
2. Hostname (required), IP, type (`Linux Server` / `Network device` / `Web/appliance`), environment, owner.
3. **Save**.

What Core does:

- `asset_id` is a slug from the hostname (`app-01` → `app-01`).
- Linux-like types get `monitoring_profile=linux-standard` and `scrape_address=<ip>:9100` when an IP is set.
- Network devices get `network-switch` and an **empty** scrape address (no `up == 0` noise).
- `source=manual`.

API equivalent: `POST /api/v1/assets` with JSON `hostname`, `ip`, `type`, `environment`, `owner`, optional `scrape_address`.

### B. Discovery (scan the management network)

Who: `engineer` or `admin` to scan and Approve. Configure CIDRs as admin on disk.

1. Set CIDRs in `config/forgesre.yml` (max **256 hosts**, loopback skipped):

```yaml
discovery:
  enabled: true
  mode: semi-automatic   # manual | semi-automatic | automatic
  cidrs: ["10.20.30.0/24"]
```

2. Recreate Core (settings load at process start).
3. Open **Discovery**. Click **Scan now**, or wait: first scan ~30s after Core start, then every **6 hours** (unless `mode: manual`).
4. Banner **NEW DEVICE DETECTED** on the dashboard until you decide.
5. **Approve** → creates an asset (`source=discovery`, id like `disc-10-20-30-41`) and sends you to the asset page. **Ignore** leaves it out of inventory.

Probe is **not nmap**. It tries TCP **22, 80, 443, 161, 9100** and guesses a role:

| Open ports | Proposed role | After Approve |
|---|---|---|
| 22 or 9100 | Possible Linux server | `scrape_address=<ip>:9100` |
| 161 | Possible network device | no scrape |
| 80 or 443 | Possible web/appliance | no scrape |

`mode: automatic` still writes an audit row (`actor=system-automatic`) then approves. Prefer semi-automatic in production.

Demo candidate `10.20.30.41` is seeded so you can click Approve without a live subnet. `./forgesre demo` puts it back if missing.

### C. External NetBox (read-sync)

ForgeSRE does **not** bundle NetBox. Point at an existing one:

```yaml
inventory:
  provider: netbox
  netbox:
    enabled: true
    mode: external
    url: "https://netbox.example.local"
```

Put the token in `secrets/secrets.env` as `NETBOX_API_TOKEN`, never in YAML. Recreate Core. On **Discovery**, **Sync NetBox** (admin), or wait for the 6-hour loop. Devices become local assets (`source=netbox`). Core **never writes** back to NetBox.

If NetBox is enabled but unreachable, **System Health** / `./doctor.sh` shows `netbox: error`.

---

## 7. Making a server actually monitored

Inventory ≠ metrics. An approved Linux host is scraped only if:

1. Something exposes Prometheus metrics at `scrape_address` (usually **node_exporter** on `:9100`).
2. Prometheus HTTP SD can see that address.

SD URL (Prometheus calls this; you can test it):

```bash
source secrets/secrets.env
curl -fsS -H "Authorization: Bearer ${ALERTMANAGER_WEBHOOK_TOKEN}" \
  http://127.0.0.1:8080/api/v1/sd/prometheus
```

Each asset with a non-empty `scrape_address` becomes a target, labeled with `asset=<asset_id>` and `job=<monitoring_profile>`.

Core itself stays on a **static** scrape so the demo HighCPU path does not depend on SD.

V0.3 bundled alert rules (`monitoring/alerts.yml`) watch **demo gauges on Core** (`forgesre_demo_cpu_percent`, `forgesre_demo_disk_percent`), not `node_*` metrics. A real server can be scraped and graphed in Grafana, but it will **not** open an incident until you add a Prometheus rule (next sections).

ForgeSRE does not install node_exporter on customer VMs. That is still your image / Ansible / whatever you already use.

---

## 8. Alerts become incidents

```
Prometheus rule fires
  → Alertmanager
  → POST /api/v1/webhooks/alertmanager  (Bearer ALERTMANAGER_WEBHOOK_TOKEN)
  → incident INC-00000N
  → playrule matched by labels.alertname
  → playbook attached
  → notification generated
  → ForgeRCA runs
```

Incident is tied to an asset when `labels.asset` or `labels.instance` equals `asset_id` or hostname. Demo alerts use `asset: forge-demo-01`.

Statuses: `OPEN` → `INVESTIGATING` (Acknowledge) → `RESOLVED` / `CLOSED`. Unacked time can move to `ESCALATED`.

Fingerprint is `alertname:asset`. A second fire of the same pair updates the open incident; it does not open a duplicate until the old one is `CLOSED`.

---

## 9. Playrules

A **playrule** is a deterministic mapping: *this Prometheus alert → this playbook + severity*. AI cannot edit playrules.

Who: `admin` (permission `write_play`). Engineers can only read.

### Create in the UI

1. Optionally create the playbook first (`/playbooks`).
2. **Playrules** → **Create playrule**.
3. Name (unique), metric, operator, value, severity, playbook.
4. **Save**. Use **Toggle** to disable without deleting.

The form stores `condition.alertname` equal to the **name** you typed. Matching on ingest is:

1. Enabled playrule whose `condition.alertname` equals Prometheus `labels.alertname` (case-insensitive), else
2. `condition.metric` equals a `metric` label or the alert name.

So if Prometheus fires `alertname: HighCPU`, the seeded rule `high-cpu` matches because its condition includes `"alertname": "HighCPU"`. If you create a UI rule named `HighCPU`, that also matches.

**Creating a playrule does not create a Prometheus alert.** Without a row in `monitoring/alerts.yml` (or another rule file), nothing fires.

### Seeded rules

| Playrule | Matches alert | Playbook |
|---|---|---|
| `high-cpu` | `HighCPU` | `CPU-HIGH` |
| `high-disk` | `FilesystemUsageHigh` | `DISK-FULL` |

API: `POST /api/v1/playrules` with `name`, `condition` (object), `playbook_id`, `severity`.

---

## 10. Playbooks

A **playbook** is a checklist shown on the incident. V0.3 **does not execute commands**. No SSH, no scripts, no auto-remediation.

Who: `admin` to create.

1. **Playbooks** → **Create playbook**.
2. Name (display, e.g. `DISK-FULL`), slug (unique id, e.g. `disk-full`).
3. Steps: **one title per line**.
4. **Save**.

Attach it by selecting it on the playrule form. When an alert matches, the incident page shows *Who / playbook*.

YAML examples in `config/examples/playbook-*.yml` are documentation for a later file-based format. They are not loaded at install.

---

## 11. Escalation and email

**Escalation** (`/escalation`) shows the seeded policy **Default warning**:

- 0 min → `team`
- 15 min → `team-lead`
- 30 min → `engineer`

A background loop every 30 seconds generates (and optionally sends) those steps while the incident stays `OPEN` / `INVESTIGATING`. The table **Generated notifications** is the outbox.

V0.3 has **no UI to add a new escalation policy**. Policies exist from seed (and could be inserted in Postgres). Playrules created in the UI currently get **no** escalation policy unless you set `escalation_policy_id` via API/DB.

Email is off until you enable it in YAML and put SMTP secrets in `secrets/secrets.env`:

```yaml
notifications:
  email:
    enabled: true
    host: smtp.example.local
    port: 587
    from: forgesre@example.local
    tls: true
```

V0.3 sends to `<target>@forgesre.local` (e.g. `team@forgesre.local`). Treat that as a lab stub: point those names at a real mailbox on your SMTP server, or leave email disabled and use the generated-notification log.

---

## 12. Incident workflow

On `/incidents/<number>`:

| Button | Who | Effect |
|---|---|---|
| Acknowledge | analyst+ | Status `INVESTIGATING`, records ack user/time |
| Resolve / Close | engineer+ | Closes the operational loop |
| Run AI investigation | analyst+ (`read_ai`) or engineer (`investigate`) | ForgeRCA; does not change the host |

Asset health on the dashboard (`healthy` / `warning` / `critical`) follows open incidents on that asset.

Grafana is for graphs. The product incident list is ForgeSRE, not Grafana Alerting.

---

## 13. AI investigation (ForgeRCA)

Open **AI Investigation** from the incident, or click **Run AI investigation**.

You get:

- Facts vs hypotheses vs anomalies
- Evidence IDs (`EV-…`) with PromQL/LogQL for engineers
- A **ForgeSRE confidence score** (not the LLM’s own number)
- Disclaimer: `AI has not modified the system.`

RCA works with `ai.enabled: false` (builtin analyst). Turn on a local LLM only if you have a GGUF or an OpenAI-compatible URL — see [`install-config.md`](install-config.md) §12.

Lab: `./forgesre demo-rca` raises filesystem usage on the **demo gauge** (does not fill a real disk).

---

## 14. Worked example: onboard a Linux server

Goal: host `app-01` at `10.10.10.50` appears under Assets and is scraped on `:9100`.

1. On `app-01`, run node_exporter listening on `0.0.0.0:9100` (or at least on the management NIC). Confirm from the ForgeSRE VM: `curl -fsS http://10.10.10.50:9100/metrics | head`.
2. Sign in as engineer/admin. **Assets** → hostname `app-01`, IP `10.10.10.50`, type `Linux Server` → **Save**.
3. Asset page should show scrape address `10.10.10.50:9100`.
4. Wait up to 30s, then check SD JSON (command in §7) contains that target.
5. On the VM: open Grafana (`:3000`) or Prometheus UI (`http://127.0.0.1:9090` from the host) and query `{asset="app-01"}` or `up{instance="10.10.10.50:9100"}`.

Optional discovery path: put `10.10.10.0/24` in `discovery.cidrs`, Scan now, Approve the `10.10.10.50` candidate instead of the manual form.

This still will **not** open `INC-…` until a Prometheus alert fires with a matching playrule (next example).

---

## 15. Worked example: new alert + playrule + playbook

Goal: when `app-01` filesystem is full, ForgeSRE opens an incident with playbook `DISK-FULL`.

**A. Prometheus rule** (this is the missing piece for real node_exporter). Edit `monitoring/alerts.yml` on the ForgeSRE host, add a group or rule, for example:

```yaml
      - alert: NodeFilesystemUsageHigh
        expr: 100 - (node_filesystem_avail_bytes{fstype!~"tmpfs|overlay"} / node_filesystem_size_bytes * 100) > 80
        for: 2m
        labels:
          severity: warning
        annotations:
          summary: "Filesystem usage high on {{ $labels.asset }}"
          description: "Disk usage is high on {{ $labels.instance }}."
```

HTTP SD already sets label `asset` from `asset_id`. Reload Prometheus (`POST http://127.0.0.1:9090/-/reload`).

**B. Playbook** (if you do not want the seeded `DISK-FULL`): **Playbooks** → name `DISK-FULL-NODE`, slug `disk-full-node`, steps one per line → **Save**.

**C. Playrule:** **Playrules** → name **must be** `NodeFilesystemUsageHigh` (same as `alertname`), metric `filesystem_usage`, playbook the one from B → **Save**.

**D. Verify:** force usage or temporarily lower the threshold, then **Incidents** should show a new `INC-…` linked to `app-01`, with the playbook name and a generated notification on **Escalation**.

If the incident has no asset, the alert `asset` / `instance` label did not match `asset_id` or hostname.

---

## 16. CLI, API, and files on disk

On the VM, from the clone directory:

```bash
./doctor.sh                 # HEALTHY / DEGRADED
./forgesre status           # compose ps
./forgesre logs core
./forgesre config           # print YAML
./forgesre demo             # HighCPU vertical slice + discovery demo IP
./forgesre demo-rca         # filesystem RCA demo gauge
./backup.sh                 # Postgres + config under $FORGESRE_DATA/backups
./update.sh                 # backup, refresh, restart, doctor
```

Useful APIs (cookie from `/login`, except webhooks/SD which use the bearer token):

| Method | Path | Who |
|---|---|---|
| POST | `/api/v1/users` | admin |
| POST | `/api/v1/assets` | engineer+ |
| GET | `/api/v1/assets` | viewer+ |
| POST | `/api/v1/discovery/scan` | engineer+ |
| POST | `/api/v1/discovery/candidates/{id}/approve` | engineer+ |
| POST | `/api/v1/playrules` | admin |
| POST | `/api/v1/playbooks` | admin |
| POST | `/api/v1/incidents/{number}/status` | engineer+ |
| POST | `/api/v1/incidents/{number}/investigate` | analyst+ |
| GET | `/api/v1/sd/prometheus` | Bearer webhook token |
| POST | `/api/v1/webhooks/alertmanager` | Bearer webhook token |

Install/config files: [`install-config.md`](install-config.md) (§6–10). Do not commit `.env`, `secrets/secrets.env`, or `data/`.

---

## 17. What V0.3 does not do yet

Say this out loud so lab expectations stay honest:

- No Kubernetes, no APM, no tracing, no auto-remediation.
- Playbooks are checklists, not executed runbooks.
- No UI to edit users, delete assets, or create escalation policies.
- Example YAML in `config/examples/` is not applied automatically.
- Bundled alert rules are demo gauges, not a full `node_exporter` ruleset.
- Discovery is a five-port TCP probe, 256 hosts max.
- NetBox is read-only and optional.
- Re-running `./install.sh` regenerates secrets.

When that is enough: install ([`install-config.md`](install-config.md)), add people (§5), add servers (§6–7), then add real alerts only when you are ready for incidents (§15).
