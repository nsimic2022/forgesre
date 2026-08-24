# ForgeSRE operations handbook

How to **operate** ForgeSRE after it is installed: users, servers, monitoring, playrules, playbooks, incidents, email, and RCA.

Install, host packages, `./forgesre test`, and configuration files: [`install-config.md`](install-config.md).  
CLI reference (everyday + advanced): [`cli.md`](cli.md).  
Verification report: [`verify.md`](verify.md).  
Local LLM (ForgeAI): [`llm.md`](llm.md).

Version notes (`v0.1.md` … `v0.7.md`) explain *what shipped*. This document explains *how you run the product*.

Code: https://github.com/nsimic2022/forgesre (`main`). UI: `http://<VM-IP>:8080`.

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
14. [Worked example: onboard a Linux or Windows server](#14-worked-example-onboard-a-linux-server)
15. [Worked example: new alert + playrule + playbook](#15-worked-example-new-alert--playrule--playbook)
16. [Operator CLI and API](#16-operator-cli-and-api)
17. [What this version does not do yet](#17-what-this-version-does-not-do-yet)

---

## 1. How the system fits together

```
Discovery / manual form / NetBox
        ↓
   Assets (inventory)
        ↓  Linux: scrape_address → Prometheus HTTP SD (node_exporter :9100)
        ↓  Windows: scrape_address → Prometheus HTTP SD (windows_exporter :9182)
        ↓  Network device + IP → snmp_exporter UDP/161 (job forgesre-snmp)
   Metrics + alert rules
        ↓  Alertmanager webhook
   Incident
        ↓  match playrule by alertname
   Playbook (guidance) + escalation email
        ↓  investigate job (webhook returns first)
   ForgeRCA (facts / hypotheses / evidence)
```

Seeded on first start:

| Object | What it is |
|---|---|
| User `FORGESRE_ADMIN_EMAIL` | `super_admin` from `secrets/secrets.env` |
| Asset `forge-demo-01` | Demo Linux host `10.10.10.20` with owner contacts (`platform@forgesre.local`, phone) and a closed HighCPU history row |
| Asset `forge-demo-win-01` | Seeded Windows lab host `10.10.10.21` (not scraped; real Windows uses windows_exporter :9182) |
| Asset `forge-demo-sw-01` | Seeded network lab switch `10.10.10.22` (not a live SNMP walk) |
| Playbooks `CPU-HIGH`, `DISK-FULL`, `HOST-UNREACHABLE`, `NETWORK-UNREACHABLE`, `WINDOWS-UNREACHABLE` | Guidance steps only |
| Playrules `high-cpu`, `high-disk`, `snmp-down`, `node-exporter-down`, `node-filesystem`, `node-cpu`, `windows-cpu`, `windows-exporter-down`, `windows-filesystem` | Demo gauges, SNMP `up`, `node_exporter`, and `windows_exporter` |
| Escalation `Default warning` | 0 / 15 / 30 minutes → generated email |
| Discovery candidate `10.20.30.41` | Demo row on `/discovery` so you can click Approve |

Lab demos (`./forgesre demo`, `./forgesre demo-rca`) fire **demo gauges on Core**, not real disk/CPU on a customer VM. After install, Dashboard **Run demo** (top right, admin) opens a closeable panel with Linux, Windows, and network lab scenarios. Rows they create are labeled **DEMO**.

---

## 2. Where work happens

Three places. Do not mix them.

| Place | You use it for | Lives in |
|---|---|---|
| **UI** (`:8080`) | Users, assets, discovery Approve/Ignore, playrules, playbooks, incident status, RCA | PostgreSQL |
| **`config/forgesre.yml`** | Discovery CIDRs, NetBox URL, AI/LLM, SMTP on/off, Loki/Grafana | File on the VM |
| **Repo / generated files** | Prometheus *alert expressions*, scrape jobs, Alertmanager webhook | `monitoring/alerts.yml`, `.env`, `secrets/secrets.env` |

YAML under `config/examples/` is the **future spec** (Playrule/Playbook/Escalation as files). V0.4 does **not** import those files. Live playrules and playbooks are created in the UI (or API) and stored in Postgres.

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

Three operating roles, plus a read-only viewer. The install user is `super_admin`.

| Role in UI | Job | Can do | Cannot |
|---|---|---|---|
| **Super admin** | Maintain the appliance | Everything, including users | — (created only by `./install.sh`) |
| **System admin** (`admin`) | Deputy for the box | Users, inventory, discovery, demos, doctor | Cannot create another super_admin |
| **Analyst** | Watch incidents, keep inventory, write the workflow | Ack/resolve incidents, **add/edit assets**, run AI (analyst view), **create playrules and playbooks** | PromQL/LogQL, Administration |
| **Engineer** | Deep RCA | Inventory, discovery Approve, full AI page (queries, evidence, history), resolve | Create playrules/playbooks, Administration |
| Viewer | Read-only | Dashboard, assets, incidents, History, System Health, Email & reports (read) | Playrules, Playbooks, Escalation, Journal, Discovery (403), any writes |

Analyst vs engineer on **AI Investigation**: same facts and likely cause. Engineer additionally sees PromQL, LogQL, evidence hashes, and similar-incident history on that page. Similar-incident history for an asset is on the **asset page** for every role that can read assets.

The UI **Create user** form cannot make another `super_admin`. Create an **Analyst** for rules/plays **and** adding hosts, an **Engineer** for deep RCA.

Login session lasts **12 hours** (httponly cookie).

---

## 4. Screen map

| Menu | URL | What you do there |
|---|---|---|
| Dashboard | `/` | Counts, doctor lights, pending discovery banner (analyst+), **Run demo** (admin, top right — Linux / Windows / network lab scenarios), recent journal reports (analyst+) |
| Assets | `/assets` | List inventory. **Add / Edit / Clone / Remove** (analyst+). Click Edit to reuse the Add form. |
| Asset detail | `/assets/<id>` | Contacts, scrape, similar-incident history. Edit / Clone / Remove / Detect (analyst+) |
| Discovery | `/discovery` | Scan, Approve / Ignore (analyst+), optional NetBox sync (admin) |
| Incidents | `/incidents` | Recent 200 Alertmanager incidents. INC id is green (resolved/closed), yellow (in progress), red (critical). **Reported to** is who received the incident report |
| History | `/history` | Last 90 days in Postgres. Filters: status, asset, `INC` number. Closed rows stay here. |
| Incident | `/incidents/INC-…` | **Acknowledge / Resolve / Close** at the top (same yellow/green as below), Who to call, **Send incident report**, **Report outbox**, audit, notes, RCA |
| Escalation | `/escalation` | Seeded policy + generated notification log (owner email when set) |
| AI Investigation | `/ai/INC-…` | ForgeRCA (green) then ForgeAI (green/yellow/red). Facts, anomalies, hypotheses |
| Playrules | `/playrules` | List, toggle, create from Prometheus presets (**analyst**) |
| Playbooks | `/playbooks` | List steps, create (**analyst**) |
| Escalation | `/escalation` | Seeded policy + generated notification log (owner email when set) |
| Journal | `/journal` | Internal process reports, split by module (ok / warn / error). Not a bash shell. |
| System Health | `/health-ui` | Same checks as `./forgesre doctor`. **Run doctor** re-probes now. Green = running, yellow = paused / starting / disabled, red = down. **Open** column (and the component name) goes to that service’s GUI or metrics. **Open Grafana** is on this page too. Prometheus/Alertmanager bind the appliance; the UI rewrites `127.0.0.1` to the host you used. |
| Email & reports | `/ops` | **Gmail** / **Outlook** send now (YAML + `SMTP_*`). **Own domain + Roundcube** is listed but not enabled until `./forgesre mailbox`. Address book, send, outbox, scheduled reports. Grafana is on System Health. |
| Administration | `/admin` | Users: click a row to **edit** or **remove**. Audit log. No browser bash — SSH or `./forgesre shell` |

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
3. Fill **Create user**: email, name, password, role (Analyst / Engineer / System admin / Viewer).
4. **Create**. The new user signs in at `/login`.
5. To change name, role, or password later: click that user in the table (left). **Save**. Empty password keeps the current one. **Remove user** deletes the account (not yourself, not the install super admin).

Same action via API (session cookie after UI login, or as the installer does):

```bash
curl -fsS -b cookies.txt -c cookies.txt -X POST http://127.0.0.1:8080/login \
  -d "email=admin@forgesre.local&password=YOUR_PASSWORD"

curl -fsS -b cookies.txt -X POST http://127.0.0.1:8080/api/v1/users \
  -H 'Content-Type: application/json' \
  -d '{"email":"ops@dc.local","name":"Ops Admin","password":"change-me","role":"admin"}'

curl -fsS -b cookies.txt -X POST http://127.0.0.1:8080/api/v1/users/2 \
  -H 'Content-Type: application/json' \
  -d '{"name":"Ops","password":"new-pass","role":"analyst"}'

curl -fsS -b cookies.txt -X POST http://127.0.0.1:8080/api/v1/users/2/delete
```

Audit rows for `user.create`, `user.update`, `user.delete`, and `login` show on `/admin`.

### Where passwords live (protected)

ForgeSRE UI users are **not** Linux/SSH accounts.

| What | Where | Protected? |
|---|---|---|
| Every UI user’s login password | PostgreSQL table `users.password_hash` | **Yes** — bcrypt hash. The plaintext is never stored and never shown in Administration. |
| Install bootstrap admin | `secrets/secrets.env` (`FORGESRE_ADMIN_EMAIL` / `FORGESRE_ADMIN_PASSWORD`) and `installation-report.md` | File on disk, mode `600`. This is a **copy for first login / CLI fallback**, not the live hash. Changing the password in Administration updates Postgres only. |
| Session cookie | httponly, 12 hours | Signed with `SECRET_KEY` (also in `secrets/secrets.env`). |

`data/` (Postgres volume), `.env`, and `secrets/` are gitignored. Do not commit them. SMTP app passwords and Grafana live in the same `secrets/secrets.env` file — different keys, same protection.

If you rotate the install admin in the UI, also edit `FORGESRE_ADMIN_PASSWORD` in `secrets/secrets.env` if you still use `./forgesre` commands that log in with that file.

---

## 6. Adding servers (inventory)

A row on **Assets** is what ForgeSRE calls a server (or switch, or appliance). You can add one **manually** or via **Discovery** (Approve). Prometheus does **not** scan the network.

- **Linux:** after the host is in inventory with `scrape_address=<ip>:9100`, Prometheus HTTP SD scrapes **node_exporter**.
- **Windows:** after the host is in inventory with type `Windows Server` and `scrape_address=<ip>:9182`, the same HTTP SD scrapes **windows_exporter**. ICMP ping is not a scrape.
- **Network device:** after the row has type `Network device` (or switch/router/firewall) **and an IP**, bundled **snmp_exporter** walks **UDP/161**. The scrape address stays empty on purpose (no exporter `up == 0` noise).

NetBox is optional and not bundled.

### A. Manual (you already know hostname + IP)

Who: **analyst**, engineer, or admin (`write_assets`).

1. **Assets** → **Add asset**.
2. Hostname (required), IP, type (**Auto (detect exporter)** is the default — or `Linux Server` / `Windows Server` / `Network device` / `Web/appliance`), environment, owner/team, **contact name, owner email, owner phone**, optional **scrape address** (`ip:9100` or `ip:9182`), notes. No extra ticketing / Zabbix / IMAP fields — those are not used.
3. **Save**. You land on the asset page (a banner explains what detect found).

**Edit / Clone / Remove** are on the list (and the asset page). Same permission as Add (`write_assets`: analyst, engineer, admin). Viewers only read.

- **Edit** opens the same Add form filled in (`/assets?edit=<id>`). Hostname, type (including Auto), IP, scrape address, owner/contact, notes, environment. `asset_id` stays (Prometheus `asset=` label and history). HTTP SD is live from this table — the next Prometheus scrape drops or rewrites the target. Core’s static demo job is not this list.
- **Clone** copies into the same form with a **new** hostname/id. Tweak before Save. Duplicate hostname or IP is rejected. NetBox id is not copied. If the source is `forge-demo-*`, the suggested name is `copy-…` (a real asset that **can** be scraped). Keep a `forge-demo-*` name only if you want another lab-only row.
- **Remove** asks for confirm. The row leaves inventory and HTTP/SNMP SD. **Incidents stay** in History with the asset link cleared (not cascade-deleted). Discovery candidates for that IP go back to **new**. Seeded `forge-demo-*` hosts cannot be removed (Dashboard demos use them).

What Core does:

- `asset_id` is a slug from the hostname at **create** (`app-01` → `app-01`). Edit can rename the hostname; `asset_id` does not change.
- **Auto (default):** Core GETs `http://<ip>:9182/metrics` and `http://<ip>:9100/metrics` (short timeout) from this appliance.
  - `windows_exporter` / `windows_` metrics → `Windows Server`, `monitoring_profile=windows-standard`, `scrape_address=<ip>:9182`.
  - `node_exporter` / `node_` metrics (`node_uname` / `node_cpu`) → `Linux Server`, `linux-standard`, `<ip>:9100`.
  - **Both:** keep a saved Linux/Windows type if the row already has one; otherwise prefer Windows `:9182` (mis-classifying Windows as Linux was the scrape miss). Override the type if the host is actually Linux.
  - **Neither:** type `Unknown`, empty scrape. ICMP ping is **not** a scrape — pick Linux/Windows yourself or install the exporter.
- Explicit **Linux Server** still defaults to `:9100` without a live probe. Explicit **Windows Server** still defaults to `:9182`.
- Network devices get `network-switch`, an **empty** scrape address, and an SNMP SD target (UDP/161 via snmp_exporter).
- Web/appliance rows are inventory only until you set a scrape address yourself. They are **not** SNMP-scraped.
- `source=manual`.
- If owner email is set, new incidents notify that address (see §11).

On the asset page, **Detect OS / scrape port** re-runs the same probe and fills type + scrape. You can override afterwards.

API: `POST /api/v1/assets` with JSON `hostname`, `ip`, `type` (`Auto (detect exporter)` to probe), `environment`, `owner`, `contact_name`, `owner_email`, `owner_phone`, `notes`, optional `scrape_address`. Detect-only: `GET /api/v1/detect-exporter?ip=`. Update: `POST /api/v1/assets/{asset_id}` (hostname, type, IP, scrape, contacts). Clone: `POST /api/v1/assets/{id}/clone`. Delete: `POST /api/v1/assets/{id}/delete`.

Similar-incident history on the asset page groups past incidents by alert/title (count, open count, last seen). Seed already puts a closed HighCPU on `forge-demo-01` so this is visible after install.

### B. Discovery (scan the management network)

Who: **analyst**, engineer, or admin to scan and Approve. Configure CIDRs as admin on disk. After Approve, fill contacts on the asset page — discovery does not guess who owns the box.

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

Probe is **not nmap**. It tries TCP **22, 80, 443, 9100, 9182** and an SNMPv2c GET on **UDP/161** (TCP/161 is skipped — that is not SNMP). When a host is alive, Core also GETs `/metrics` on **:9182** and **:9100** (same detect as Add Asset):

| Probe | Proposed role | After Approve |
|---|---|---|
| `:9182/metrics` has `windows_` | Possible Windows server | `scrape_address=<ip>:9182` (windows_exporter) |
| `:9100/metrics` has `node_` | Possible Linux server | `scrape_address=<ip>:9100` (node_exporter) |
| Both exporters | Prefer saved type; else Windows `:9182` | matching scrape |
| TCP 9100/9182 open, no exporter text | Caveat in the role (`no … /metrics`) | inventory; **no** scrape until /metrics works |
| UDP/161 SNMP GET succeeds (even if SSH is open) | Possible network device | SNMP UDP/161 (no HTTP exporter scrape) |
| 22 only | Possible Linux server | inventory only — **no** `:9100` until node_exporter answers |
| 80 or 443 | Possible web/appliance | inventory only |

TCP open on 9100 or 9182 is **not** an OS pick. ICMP ping is not used for OS.

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

Inventory ≠ metrics. Pick the right exporter:

### Linux (node_exporter)

An approved Linux host is scraped only if:

1. Something exposes Prometheus metrics at `scrape_address` (usually **node_exporter** on `:9100`).
2. Prometheus HTTP SD can see that address.

```bash
source secrets/secrets.env
curl -fsS -H "Authorization: Bearer ${ALERTMANAGER_WEBHOOK_TOKEN}" \
  http://127.0.0.1:8080/api/v1/sd/prometheus
```

Each asset with a non-empty `scrape_address` becomes a target, labeled with `asset=<asset_id>` and `job=<monitoring_profile>`. Seeded `forge-demo-*` hosts are lab-only and are **not** in this list.

Core itself stays on a **static** scrape so the demo HighCPU path does not depend on SD.

ForgeSRE does not install node_exporter on customer VMs. That is still your image / Ansible / whatever you already use.

Confirm from the ForgeSRE VM with `./forgesre ping` (ICMP + `:9100/metrics`). Ping alone is not a scrape.

### Windows (windows_exporter)

Prometheus **node_exporter is Linux**. A Windows host is scraped only if:

1. **windows_exporter** (or equivalent) exposes `/metrics` at `scrape_address` (typical **`:9182`**).
2. The asset type is `Windows Server` (profile `windows-standard`) so HTTP SD labels `job=windows-standard`.
3. Windows Firewall / NSX allows **TCP 9182** from the ForgeSRE VM.

ICMP ping from the appliance only proves L3. ForgeSRE "seeing" the host needs the `:9182` scrape.

```bash
./forgesre ping                # all real assets
./forgesre ping win-01         # one row
```

| ICMP | METRICS | What it means |
|---|---|---|
| PASS | FAIL | Host is up; exporter is down, **TCP 9182** is firewalled, or scrape is still **:9100**. |
| FAIL | FAIL | Wrong IP or the host is down. |
| PASS | PASS | windows_exporter answers. Wait ~30s, then `./forgesre sd`. |

If you added the host as `Linux Server`, it will be scraped on `:9100` looking for node_exporter — that will not see windows_exporter. Open the asset → **Detect OS / scrape port** (or set type to **Windows Server** and scrape address to `<ip>:9182`). Detect does not rewrite custom scrape addresses unless you click it or save type **Auto**.

`node_exporter` on Windows (WSL / Cygwin) is rare. If you really run it, add the host as `Linux Server` or set scrape to `<ip>:9100` by hand.

Dashboard **Run demo → Windows CPU** still uses lab asset `forge-demo-win-01`. That row is **not** scraped. Do not confuse it with a real Windows box.

V0.6+ bundled alert rules (`monitoring/alerts.yml`) watch:

- **Demo gauges on Core** (`forgesre_demo_cpu_percent`, `forgesre_demo_disk_percent`) — `forge-demo-01` only
- **node_exporter** (`NodeExporterDown`, `NodeFilesystemUsageHigh`, `NodeCPUHigh`) for HTTP SD targets with `job=linux-standard`
- **windows_exporter** (`WindowsExporterDown`, `WindowsFilesystemUsageHigh`, `WindowsCPUHigh`) for HTTP SD targets with `job=windows-standard`
- **SNMP** (`SnmpDeviceUnreachable`, `NetworkInterfaceDown`)

Site-specific extras go in `monitoring/alerts.local.yml` (copy the `.example`; gitignored), then `./forgesre render-monitoring`.

ForgeRCA for a real Linux host queries `node_*` labeled `asset=<asset_id>`. A real Windows host queries `windows_*`. It does **not** reuse the demo CPU/disk gauges.

### Network device (snmp_exporter)

Bundled container `snmp-exporter` listens on `127.0.0.1:9116` (host network). Prometheus job `forgesre-snmp` asks Core for SNMP targets, then tells the exporter to walk each device IP.

```bash
./forgesre snmp
source secrets/secrets.env
curl -fsS -H "Authorization: Bearer ${ALERTMANAGER_WEBHOOK_TOKEN}" \
  http://127.0.0.1:8080/api/v1/sd/snmp
```

Empty JSON `[]` is normal until a **Network device** row has an IP. Linux and Windows HTTP exporter hosts never appear here.

From the ForgeSRE VM the exporter speaks **UDP/161** to the device. Allow that outbound. The device ACL must allow this host. Community is `SNMP_COMMUNITY` in `secrets/secrets.env` (lab default `public`). After you change it:

```bash
./forgesre render-monitoring
docker compose up -d snmp-exporter
```

If the walk fails, Prometheus `up{job="forgesre-snmp"}` is 0. Alert `SnmpDeviceUnreachable` fires after 2 minutes and matches playrule `snmp-down` (playbook `NETWORK-UNREACHABLE`). That is community/ACL/device-down — not “Prometheus is down”.

CLI and doctor:

```bash
./forgesre doctor          # component snmp
./forgesre logs snmp-exporter
./forgesre help snmp
```

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
  → investigate job enqueued (worker runs ForgeRCA; webhook does not wait)
```

Incident is tied to an asset when `labels.asset` or `labels.instance` equals `asset_id` or hostname. Demo alerts use `asset: forge-demo-01`.

Statuses: `OPEN` → `INVESTIGATING` (Acknowledge) → `RESOLVED` / `CLOSED`. Unacked time can move to `ESCALATED`.

Fingerprint is `alertname:asset`. A second fire of the same pair updates the open incident; it does not open a duplicate until the old one is `CLOSED`. A **resolved** alert closes the open incident and does not create a new one. New numbers look like `INC-0134_16.08.2026_09:13` (short seq + local date/time). Older `INC-000012` rows stay valid. Sequence is still `max(seq)+1`, not `count(*)+1`. TAB in `./forgesre` completes those ids after `incidents` / `history`.

Check the RCA queue with `./forgesre jobs`. If a job is `error`, open Console (`/journal`) module `rca`.

---

## 9. Playrules

A **playrule** is a deterministic mapping: *this Prometheus alert → this playbook + severity*. AI cannot edit playrules.

Who: **analyst** (permission `write_play`). Engineers can read, not create.

### Create in the UI

1. Optionally create the playbook first (`/playbooks`).
2. **Playrules** → **Create playrule**.
3. Name (unique), metric, operator, value, severity, playbook.
4. **Save**. Use **Toggle** to disable without deleting.

The form stores `condition.alertname` equal to the **name** you typed. Matching on ingest is:

1. Enabled playrule whose `condition.alertname` equals Prometheus `labels.alertname` (case-insensitive), else
2. `condition.metric` equals a `metric` label or the alert name.

So if Prometheus fires `alertname: HighCPU`, the seeded rule `high-cpu` matches because its condition includes `"alertname": "HighCPU"`. If you create a UI rule named `HighCPU`, that also matches.

**Creating a playrule does not create a Prometheus alert.** Bundled rules live in `monitoring/alerts.yml`. Extra rules: `monitoring/alerts.local.yml`, then `./forgesre render-monitoring`.

### Seeded rules

| Playrule | Matches alert | Playbook |
|---|---|---|
| `high-cpu` | `HighCPU` | `CPU-HIGH` |
| `high-disk` | `FilesystemUsageHigh` | `DISK-FULL` |
| `snmp-down` | `SnmpDeviceUnreachable` | `NETWORK-UNREACHABLE` |
| `node-exporter-down` | `NodeExporterDown` | `HOST-UNREACHABLE` |
| `node-filesystem` | `NodeFilesystemUsageHigh` | `DISK-FULL` |
| `node-cpu` | `NodeCPUHigh` | `CPU-HIGH` |

API: `POST /api/v1/playrules` with `name`, `condition` (object), `playbook_id`, `severity`.

---

## 10. Playbooks

A **playbook** is a checklist shown on the incident. V0.3 **does not execute commands**. No SSH, no scripts, no auto-remediation.

Who: **analyst** to create.

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

If the incident’s asset has **owner email**, every step is addressed to that email (demo: `platform@forgesre.local`). The body includes contact name and phone. Policy roles (`team` / `team-lead` / `engineer`) stay in the body as the step name. If owner email is empty, ForgeSRE falls back to `<role>@forgesre.local`.

Incident reports and escalation mail are **multipart** (`text/plain` + `text/html`). Gmail and Outlook show the HTML (severity color bar, DEMO banner when the asset is `forge-demo-*`, ForgeRCA sections). The Report outbox on the incident page still stores the plain-text body. **Send email** on `/ops` (Compose) stays as the operator typed it — ForgeSRE does not rewrite that text into HTML tables.

This version has **no UI to add a new escalation policy**. Policies exist from seed (and could be inserted in Postgres). Playrules created in the UI (or API) attach the seeded **Default warning** policy when none is set. The 30s loop reads that policy’s `after_minutes` / `target` steps.

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

Leave SMTP **disabled** only if you want the on-box outbox and no real mail.

ForgeSRE **sends**. It does **not** receive inbound mail into the UI. Humans read and reply in Gmail, Outlook, or (later) Roundcube.

**Now (Core, unchanged):** Gmail or Outlook / Microsoft 365. Same YAML + `SMTP_*` secrets as before. Incident send, escalation, and `/ops` keep working.

**Later (Compose profile `mailbox`, off at install):** when you own a domain, `./forgesre mailbox` starts Postfix + Dovecot + Roundcube. That does **not** rewrite Core SMTP unless you pass `--bind-core`.

### Gmail

1. Security → 2-Step Verification → **App passwords** → 16 characters (not your login password).
2. `config/forgesre.yml`:

```yaml
notifications:
  email:
    enabled: true
    host: smtp.gmail.com
    port: 587
    from: you@gmail.com
    tls: true
```

3. `secrets/secrets.env`:

```bash
SMTP_USERNAME=you@gmail.com
SMTP_PASSWORD=xxxx xxxx xxxx xxxx
```

4. `./forgesre update`. **Send incident report**. Outbox must show `sent`. `failed` = wrong app password or outbound 587 blocked. `generated` = `enabled` is still false.

From-address must be the same Gmail account.

### Outlook / Microsoft 365

Same keys, different host. Replies stay in Outlook.

```yaml
notifications:
  email:
    enabled: true
    host: smtp.office365.com
    port: 587
    from: you@outlook.com
    tls: true
```

```bash
SMTP_USERNAME=you@outlook.com
SMTP_PASSWORD=...
```

Work / school Microsoft 365: same host `smtp.office365.com`, your work address. Consumer Outlook.com / Hotmail: same host (or `smtp-mail.outlook.com` if that is what Microsoft shows for the account). MFA accounts need an app password.

### Own domain + Roundcube (not enabled)

Compose services `mailserver` and `roundcube` sit in `docker-compose.yml` under profile **`mailbox`**. Install does **not** start them. Core, incidents, and Gmail/Outlook SMTP stay as they are.

When you have bought a domain and want the on-box server:

```bash
# optional: MAIL_DOMAIN=ops.example.com in .env first (default forgesre.local)
./forgesre mailbox
```

That starts Postfix + Dovecot + Roundcube (`:8081`) and writes `MAILBOX_*` in secrets. It does **not** change `config/forgesre.yml` or `SMTP_PASSWORD`. Gmail/Outlook keep sending.

`--bind-core` is the later switch if you want Core to submit to `127.0.0.1:587` instead of Gmail/Outlook.

Internet receive still needs MX at this host and **TCP/25**. Many ISPs/clouds block port 25.

Do not combine with Mailpit (`COMPOSE_PROFILES=mail`).

### Lab-only: Mailpit (not production)

Skip this if you want real mail. Mailpit is a catcher on the VM (`COMPOSE_PROFILES=mail`, YAML `127.0.0.1:1025`, `tls: false`). Messages never leave the box and never appear in Gmail, Outlook, or Roundcube.

---

## 12. Incident workflow

On `/incidents/<number>`:

| Button | Who | Effect |
|---|---|---|
| Acknowledge | analyst+ | Status `INVESTIGATING`, records ack user/time |
| Resolve / Close | analyst+ (`write_incidents`) | Closes the operational loop; records who resolved |
| Run AI investigation | analyst+ (`read_ai`) or engineer (`investigate`) | ForgeRCA; does not change the host |
| **Send incident report** | analyst / engineer / admin | Emails the current INC snapshot when SMTP is on (`sent`) as HTML + plain text. If SMTP is off, stores `generated` in **Report outbox** (plain body). Replies arrive in the real mailbox, not in ForgeSRE |

The same page lists **email notifications** for this `INC` (bodies, `generated` vs `sent`), **who did what** (audit: ack, resolve, notes), and **operator notes** (what a person actually did, e.g. cleaned WAL). Notes are not a ticket thread and not RCA.

**History** (`/history`) is the 90-day lookback of the same Postgres incidents. Use it for closed work and date filters. Escalation is still the policy + recent mail log. Console (`/journal`) is still process reports, not incident history.

Asset health on the dashboard (`healthy` / `warning` / `critical`) follows open incidents on that asset.

Grafana is for graphs. The product incident list is ForgeSRE, not Grafana Alerting.

---

## 13. AI investigation (ForgeRCA)

Open **ForgeRCA investigation** from the incident, or click **Run AI investigation**.

The button runs **builtin ForgeRCA immediately** and opens Summary → Root cause → Recommended actions → Facts → Anomalies → Candidate causes → Limitations. Two pills sit at the top: **ForgeRCA** (green when builtin has a result) and **ForgeAI** (green if the LLM rewrote the prose, yellow while the rewrite runs, red if the LLM is off or unreachable). If `ai.enabled` is on, refresh later for ForgeAI. Do not mash Run now.

You get:

- Facts vs hypotheses vs anomalies
- Evidence IDs (`EV-…`) with PromQL/LogQL for engineers
- A **ForgeSRE confidence score** (not the LLM’s own number)
- Disclaimer: `AI has not modified the system.`

RCA works with `ai.enabled: false` (builtin analyst). To use a local LLM, download the GGUF (not in git) with `./forgesre fetch-llm`. Implementation (hardware, Compose profile `ai`, offline GGUF, jobs, debug): [`llm.md`](llm.md).

Alertmanager ingest **enqueues** an investigate job. The webhook does not wait on the LLM. `./forgesre jobs` lists pending / running / done / error. Demo (`./forgesre demo`) still runs RCA inline so the first-hour path is immediate.

Queries are **per asset**. `forge-demo-01` uses Core demo gauges. A real Linux host uses `node_cpu_seconds_total` / `node_filesystem_*` with `asset="<id>"`. A real Windows host uses `windows_cpu_time_total` / `windows_logical_disk_*`. A network device uses `up{job="forgesre-snmp",asset="<id>"}`. Demo CPU/disk numbers are never overlaid on another host.

Lab: `./forgesre demo-rca` raises filesystem usage on the **demo gauge** (does not fill a real disk). `./forgesre demo-reset` puts the gauges back.

---

## 14. Worked example: onboard a Linux server

Goal: host `app-01` at `10.10.10.50` appears under Assets and is scraped on `:9100`.

1. On `app-01`, run node_exporter listening on `0.0.0.0:9100` (or at least on the management NIC). From the ForgeSRE VM: `./forgesre ping app-01` (or `curl -fsS http://10.10.10.50:9100/metrics | head`). ICMP ping alone is not a scrape.
2. Sign in as analyst/engineer/admin. **Assets** → hostname `app-01`, IP `10.10.10.50`, leave type **Auto (detect exporter)** (or pick `Linux Server`), owner email/phone of who to call → **Save**.
3. Asset page should show scrape address `10.10.10.50:9100` and the contacts. Edit them later if the owner changes.
4. Wait up to 30s, then check SD JSON (command in §7) contains that target.
5. On the VM: open Grafana (`:3000`) or Prometheus UI (`http://127.0.0.1:9090` from the host) and query `{asset="app-01"}` or `up{instance="10.10.10.50:9100"}`.

Optional discovery path: put `10.10.10.0/24` in `discovery.cidrs`, Scan now, Approve the `10.10.10.50` candidate instead of the manual form.

This still will **not** open `INC-…` until a Prometheus alert fires with a matching playrule. Bundled `NodeExporterDown` / `NodeFilesystemUsageHigh` / `NodeCPUHigh` already match seeded playrules once node_exporter is scraped. Custom thresholds: §15.

### Windows server (windows_exporter)

Goal: host `win-01` at `10.10.10.60` appears under Assets and is scraped on `:9182`.

1. On `win-01`, install [windows_exporter](https://github.com/prometheus-community/windows_exporter) listening on `0.0.0.0:9182` (default). Allow **TCP 9182** from the ForgeSRE VM in Windows Firewall.
2. From the **ForgeSRE VM** (the box that runs Prometheus), not from a laptop:

```bash
./forgesre ping win-01
# equivalent manual checks:
ping -c 3 10.10.10.60
curl -sS -m 5 http://10.10.10.60:9182/metrics | head
```

ICMP PASS is L3 only. METRICS PASS (or curl printing `# HELP` / `windows_`) is the scrape. ICMP PASS / METRICS FAIL → exporter not running, firewall **TCP 9182**, or the row is still type Linux Server (`:9100`).

3. Sign in as analyst/engineer/admin. **Assets** → hostname `win-01`, IP `10.10.10.60`, leave type **Auto (detect exporter)** (or pick **Windows Server**), owner email/phone → **Save**. Detect should set Windows Server and `:9182`. If the host was already saved as Linux, open it and click **Detect OS / scrape port**.
4. Asset page should show scrape address `10.10.10.60:9182` and profile `windows-standard`.
5. Wait up to 30s. `./forgesre sd` (or the curl in §7) should list that target with `job=windows-standard`.
6. Prometheus (on the VM): `up{instance="10.10.10.60:9182"}` or `{asset="win-01"}`.

Do not use Dashboard **Run demo → Windows CPU** as proof of live scrape. That opens a DEMO incident on `forge-demo-win-01` without talking to windows_exporter.

Bundled `WindowsExporterDown` / `WindowsFilesystemUsageHigh` / `WindowsCPUHigh` match seeded playrules `windows-exporter-down` / `windows-filesystem` / `windows-cpu`.

### Network switch (SNMP)

Goal: `core-sw-01` at `10.30.1.1` is walked by snmp_exporter.

1. On the switch, enable SNMPv2 read-only with a community the ForgeSRE VM may use. ACL: allow the ForgeSRE host on **UDP/161**.
2. From the ForgeSRE VM: `./forgesre doctor` should show `snmp` ok. If not: `docker compose up -d snmp-exporter`.
3. **Assets** → hostname `core-sw-01`, IP `10.30.1.1`, type **Network device**, owner email of who to call → **Save**.
4. Asset page should say it is polled by snmp_exporter. Scrape address stays empty.
5. `./forgesre snmp` — SD JSON contains `10.30.1.1`.
6. Wait ~30s. Prometheus query (on the VM): `up{job="forgesre-snmp",asset="core-sw-01"}`. `1` = walk succeeded. `0` after 2m opens `SnmpDeviceUnreachable`.

Change community in `secrets/secrets.env` (`SNMP_COMMUNITY`), then `./forgesre render-monitoring` and `docker compose up -d snmp-exporter`. Do not re-run `./install.sh`.

---

## 15. Worked example: new alert + playrule + playbook

Goal: when `app-01` filesystem is full, ForgeSRE opens an incident with playbook `DISK-FULL`.

**A. Prometheus rule.** Bundled `NodeFilesystemUsageHigh` already watches `node_exporter` at 90%. For a local threshold or extra alerts, copy `monitoring/alerts.local.yml.example` to `monitoring/alerts.local.yml` (gitignored) and add a group. Then:

```bash
./forgesre render-monitoring
curl -fsS -X POST http://127.0.0.1:9090/-/reload
```

Do **not** edit only `monitoring/alerts.yml` on a live box if you use generated config — `render-monitoring` copies the repo file plus `alerts.local.yml` into `$FORGESRE_DATA/generated/alerts.yml`.

HTTP SD already sets label `asset` from `asset_id`.

**B. Playbook** (if you do not want the seeded `DISK-FULL`): **Playbooks** → name `DISK-FULL-NODE`, slug `disk-full-node`, steps one per line → **Save**.

**C. Playrule:** seeded `node-filesystem` already matches `NodeFilesystemUsageHigh`. For a custom alert name, **Playrules** → name **must be** the Prometheus `alertname` → **Save**. New playrules get the default escalation policy.

**D. Verify:** force usage or temporarily lower the threshold in `alerts.local.yml`, then **Incidents** should show a new `INC-…` linked to `app-01`, with the playbook name, **Who to call**, and a generated notification on **Escalation** addressed to the asset owner email if you filled it. RCA appears a few seconds later (`./forgesre jobs`).

If the incident has no asset, the alert `asset` / `instance` label did not match `asset_id` or hostname.

---

## 16. Operator CLI and API

On the VM, from the clone directory, `./forgesre` is the operator CLI. `./forgesre help` lists commands. `./forgesre help <command>` prints explanation and examples.

`./forgesre` with no extra words opens a prompt (`forgesre>`). After that you type the **full** command (`journal`, `incidents`, `history`, `doctor`, `help snmp`) without repeating `./forgesre`. Leave with `quit`, `exit`, or Ctrl-D — `./forgesre help quit`. `./f` is the same binary with a shorter filename (`./f journal`). Command names are not one-letter aliases.

### SSH from your laptop

ForgeSRE does **not** speak SSH of its own. You SSH into the Ubuntu VM, then use the CLI on localhost.

```bash
ssh you@forgesre-vm
cd /path/to/forgesre
./forgesre login                 # ForgeSRE user, e.g. engineer@dc.local
./forgesre whoami
./forgesre                       # prompt
incidents                        # red / yellow / green board
incidents INC-000012             # mail, audit, notes for that INC
history --days 90
quit
```

Two different logins:

1. **Linux SSH** — OS account on the VM (`ssh engineer@vm`). Your sysadmin creates this.
2. **ForgeSRE role** — UI user created under Administration (`engineer` / `analyst` / `viewer`). `./forgesre login` stores `data/cli.session`. Without that cookie, the CLI uses the install admin from `secrets.env` if the file is readable.

Colors (TTY only; `FORGESRE_COLOR=1` to force, `=0` to disable): **red** critical/open, **yellow** in progress / warning, **green** resolved/closed.

```bash
./forgesre                  # interactive prompt
./forgesre login
./forgesre incidents
./forgesre incidents INC-000012
./f journal
./forgesre help                 # overview
./forgesre help quit            # leave the forgesre> prompt
./forgesre help snmp            # one command
./forgesre help ping            # ICMP vs /metrics
./forgesre help tls             # optional HTTPS
./forgesre doctor               # short HEALTHY / DEGRADED
./forgesre ping                 # ICMP + exporter /metrics (alias: probe)
./forgesre ping win-01
./forgesre test                 # detailed report → data/reports/
./forgesre status               # compose ps
./forgesre logs core
./forgesre logs snmp-exporter
./forgesre config               # print YAML
./forgesre assets               # inventory table (alias: inventory)
./forgesre snmp                 # exporter HTTP check + SNMP SD JSON
./forgesre sd                   # Linux + Windows HTTP SD, then SNMP HTTP SD
./forgesre incidents            # colored board; INC-… opens one row
./forgesre login                # ForgeSRE UI user (engineer/analyst)
./forgesre whoami
./forgesre logout
./forgesre history              # 90-day lookback (filters; INC-… for mail/audit/notes)
./forgesre jobs                 # background RCA queue
./forgesre render-monitoring    # rewrite generated prometheus/alertmanager/snmp/alerts.yml
./forgesre journal              # last process reports
./forgesre journal snmp
./forgesre journal inventory
./forgesre demo                 # HighCPU + owner notification + similar history
./forgesre demo-rca             # filesystem RCA demo gauge
./forgesre demo-reset           # lower demo gauges
./forgesre secrets-check
./forgesre fetch-llm            # GGUF download (~9 GB, not in git)
./forgesre backup
./forgesre backup --no-secrets
./forgesre update               # backup + render-monitoring + compose up + doctor
./forgesre mailbox              # optional Roundcube later; does not rewrite Core SMTP
./forgesre version
```

Examples (existing VM after `git pull origin main` — **do not** run `./install.sh`):

```bash
./forgesre update
./forgesre secrets-check
./forgesre snmp
```

Add a switch, then confirm it is an SNMP target:

```bash
./forgesre assets
./forgesre snmp
# expect 10.x.x.x in the JSON list, not in /api/v1/sd/prometheus
```

Useful APIs (cookie from `/login`, except webhooks/SD which use the bearer token):

| Method | Path | Who |
|---|---|---|
| POST | `/api/v1/users` | admin (create) |
| POST | `/api/v1/users/{id}` | admin (edit; omit password to keep it) |
| POST | `/api/v1/users/{id}/delete` | admin (cannot delete self or super_admin) |
| POST | `/api/v1/assets` | analyst+ |
| POST | `/api/v1/assets/{id}` | analyst+ (edit hostname/type/IP/scrape/contacts) |
| POST | `/api/v1/assets/{id}/clone` | analyst+ |
| POST | `/api/v1/assets/{id}/delete` | analyst+ (not `forge-demo-*`; incidents stay, FK cleared) |
| GET | `/api/v1/detect-exporter` | analyst+ (`?ip=` — :9182 then :9100 /metrics) |
| GET | `/api/v1/assets` | viewer+ |
| POST | `/api/v1/discovery/scan` | analyst+ |
| POST | `/api/v1/discovery/candidates/{id}/approve` | analyst+ |
| POST | `/api/v1/playrules` | analyst+ |
| POST | `/api/v1/playbooks` | analyst+ |
| GET | `/api/v1/history` | viewer+ (`read_incidents`; `days`, `status`, `asset`, `number`) |
| GET | `/api/v1/incidents/{number}` | viewer+ (includes notifications, audit, notes) |
| POST | `/api/v1/incidents/{number}/notes` | analyst+ |
| POST | `/api/v1/incidents/{number}/status` | analyst+ |
| POST | `/api/v1/incidents/{number}/investigate` | analyst+ |
| GET | `/api/v1/journal` | analyst+ (`read_play`; module, status, q) |
| POST | `/api/v1/journal` | admin (install writes one row here) |
| GET | `/api/v1/jobs` | analyst+ (`read_play`) |
| GET | `/api/v1/system/doctor` | login **or** Bearer webhook token |
| GET | `/api/v1/sd/prometheus` | Bearer webhook token (Linux node_exporter :9100 and Windows windows_exporter :9182) |
| GET | `/api/v1/sd/snmp` | Bearer webhook token (network devices) |
| POST | `/api/v1/webhooks/alertmanager` | Bearer webhook token |

Install/config files: [`install-config.md`](install-config.md). Do not commit `.env`, `secrets/secrets.env`, or `data/`.

**Console** (`/journal`) is the internal process journal: seed, inventory, discovery, snmp, incidents, RCA, notifications, demo, install. Each action writes a short ok/warn/error report. Rows are split by module and pruned automatically (~200 per module) so search stays small. This is not a dump of Docker logs and not the Administration audit log (who clicked what). Prometheus HTTP SD is **not** journaled on every scrape (that would flood the table).

Root wrappers `./doctor.sh`, `./test.sh`, `./backup.sh`, `./update.sh`, `./install.sh` still work; they call the same scripts as `./forgesre`.

---

## 17. What this version does not do yet

Say this out loud so lab expectations stay honest:

- No Kubernetes, no APM, no tracing, no auto-remediation.
- Playbooks are checklists, not executed runbooks.
- No UI to create escalation policies. Assets can be added, edited, cloned, and removed (analyst+). Seeded `forge-demo-*` rows cannot be removed. Users can be edited and removed on Administration (not the install super admin, not yourself).
- Example YAML in `config/examples/` is not applied automatically.
- Bundled alert rules include demo gauges, SNMP `up` / interface-down, a small `node_exporter` set (down / disk 90% / CPU 95%), and a small `windows_exporter` set (down / volume 90% / CPU 90%). Extra rules go in `alerts.local.yml`.
- Discovery is TCP 22/80/443/9100/9182 plus SNMP GET on UDP/161, 256 hosts max. It does not use TCP/161. SNMP *polling* is still snmp_exporter after Approve.
- Viewer cannot open Playrules, Playbooks, Escalation, Console, or Discovery (403).
- Optional TLS is an example Caddyfile, not a default container.
- NetBox is read-only and optional.
- Re-running `./install.sh` regenerates secrets. Core will not start on shipped default `SECRET_KEY` / webhook token (`FORGESRE_DEV=1` is tests/lab only).

When that is enough: install ([`install-config.md`](install-config.md)), add people (§5), add servers (§6–7), then add real alerts only when you are ready for incidents (§15). First-hour lab path: Dashboard **Run demo** → HighCPU on `forge-demo-01` (DEMO pill) → Who to call / Escalation. CLI: `./forgesre demo`.
