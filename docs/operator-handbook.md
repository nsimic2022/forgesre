# ForgeSRE operations handbook

How to **operate** ForgeSRE after it is installed: users, servers, monitoring, playrules, playbooks, incidents, email, and RCA.

Install, host packages, `./forgesre test`, and configuration files: [`install-config.md`](install-config.md).  
CLI reference (everyday + advanced): [`cli.md`](cli.md).  
Verification report: [`verify.md`](verify.md).  
Local LLM (ForgeAI): [`llm.md`](llm.md).

Version notes (`v0.1.md` … `v0.7.md`) explain *what shipped*. This document explains *how you run the product*. Commands live in [`cli.md`](cli.md). Learning order: [`docs/README.md`](README.md).

Code: https://github.com/nsimic2022/forgesre (`main`). UI: `http://<VM-IP>:8080`.

ForgeSRE does **not** replace Prometheus, Grafana, Loki, or NetBox. It sits on top of them. AI is **read-only**: it never SSH-es, never runs playbooks, never writes NetBox.

---

0. [How to learn the platform](#0-how-to-learn-the-platform)
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

## 0. How to learn the platform

Follow this order on a live box. This handbook is **why and when**. Typed commands: [`cli.md`](cli.md).

1. **Install or update.** New VM: [`install-config.md`](install-config.md) then `./install.sh`. Live box: `git pull origin main && ./forgesre update`. Never `./install.sh` again (it regenerates passwords).
2. **Login.** `http://<VM-IP>:8080` with `installation-report.md` / `secrets/secrets.env` (§5).
3. **System Health** (`/health-ui`) = `./forgesre doctor`. Open Grafana only here. Alarm path is Prometheus → Alertmanager → Core. NetBox UI up with API 403 is yellow **warn**, not paused. SNMP with no Network device + IP is **paused (no SNMP targets)** (yellow — leave it).
4. **Assets** (`/assets`). Local inventory is the monitoring source of truth. Add / Edit / Verify.
5. **Discovery vs NetBox** (`/discovery`). **Scan now** unions live `discovery.cidrs` with **auto-detected connected IPv4 nets** (real prefixlen — not a hardcoded `/24`). Candidate table: open ports, node_exporter, windows_exporter, SNMP. **Sync NetBox** sits beside Scan now (read-only from bundled `:8001`). Empty NetBox is **yellow No devices** — that is OK. Approve or Ignore; Manual Assets stay SoT. Manual Assets stay SoT.
6. **Incidents** (`/incidents`) then **History** (`/history`). IDs look like `INC-0134_16.08.2026_09:13`.
7. **ForgeRCA vs ForgeAI.** Open ForgeRCA on the incident. ForgeRCA (Python) always first. ForgeAI only rewrites prose (optional, default 90s timeout, one worker thread).
8. **verify ≠ doctor ≠ test.** `./forgesre verify` = live inventory path. `./forgesre doctor` = Health lights. `./forgesre test` = appliance report → `data/reports/`.
9. **Backup.** Administration or `./forgesre backup`. Restore is not silent (`--yes`).
10. **CLI cheat sheet.** [`cli.md`](cli.md). On the box: `./forgesre help`.

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
| Playbooks `CPU-HIGH`, `DISK-FULL`, `MEMORY-HIGH`, `HOST-UNREACHABLE`, `NETWORK-UNREACHABLE`, `WINDOWS-UNREACHABLE` | Guidance steps only |
| Playrules `high-cpu`, `high-disk`, `snmp-down`, `node-exporter-down`, `node-filesystem`, `node-cpu`, `node-memory`, `windows-cpu`, `windows-exporter-down`, `windows-filesystem`, `windows-memory` | Demo gauges, SNMP `up`, `node_exporter`, and `windows_exporter` |
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

YAML under `config/examples/` is the **future spec** (Playrule/Playbook/Escalation as files). V0.7 does **not** import those files. Live playrules and playbooks are created in the UI (or API) and stored in Postgres.

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

Left nav is a constant dark shell (does not follow the theme). The control at the bottom cycles **Light / Dark / System**; System follows the OS for the main pane only. After a CSS change, hard-refresh so `/static/app.css` is not served from cache. Operator list tables (dashboard recent incidents, incidents, history, journal, mail outbox, reports, assets, discovery) show **10 rows per page** with Previous / 1 / 2 / 3 / Next.

| Menu | URL | What you do there |
|---|---|---|
| Dashboard | `/` | Counts, HOST DOWN banner, pending discovery banner (analyst+), **Run demo** (admin, top right), recent incidents. Full doctor grid is **System Health**, not here. |
| Assets | `/assets` | List inventory. **Add / Edit / Clone / Remove** (analyst+). One sentence: Verify ≠ doctor ≠ `./forgesre test`. |
| Asset detail | `/assets/<id>` | Contacts, scrape, similar-incident history. **One Edit Alarms** on the metrics panel (not per tile). |
| Discovery | `/discovery` | Suggests management `/24` from this VM; **Confirm & scan** / **Scan now** (TCP/SNMP/HTTP, not nmap; empty cidrs = no scan) side-by-side with **Sync NetBox** (read-only, admin). Columns: open ports, node_exporter, windows_exporter, SNMP. Demo IP `10.20.30.41` is a DEMO lab seed. |
| Incidents | `/incidents` | **Open/firing** list (filter: open-only / last days). INC id is green (resolved/closed), yellow (in progress), red (critical). Archive is **History**. |
| History | `/history` | Archive: last 90 days in Postgres. Filters: status, asset, `INC` number. Closed rows stay here. |
| Incident | `/incidents/INC-…` | **Acknowledge / Resolve / Close**, Who to call, **Open ForgeRCA** (primary CTA → `/ai/INC-…`), **Send incident report**. Mail outbox is `/ops#mail`. ForgeRCA/ForgeAI pills stay. |
| AI Investigation | `/ai/INC-…` | ForgeRCA (green) then ForgeAI (green/yellow/red). Facts, anomalies, hypotheses. Empty host logs: honest limitation (Alloy ships appliance/Core logs only). |
| Playrules | `/playrules` | Map `alertname` → playbook. Do **not** create Prom rules. `alerts.yml` is PromQL; asset Alarms are overlay. |
| Playbooks | `/playbooks` | List steps, create (**analyst**) |
| Escalation | `/escalation` | Seeded **Default warning**, create policy (Save + Cancel). Mail: `/ops#mail`. |
| Journal | `/journal` | Internal process reports, split by module (ok / warn / error). Not a bash shell. |
| System Health | `/health-ui` | Same checks as `./forgesre doctor`. **Open Grafana** lives **only here** (not left nav, not the alarm path). Alarm path: Prometheus → Alertmanager → Core. Grafana down is yellow (graphs only), not a Prometheus FAIL. Prom/AM errors name `:9090` / `:9093`, not “Prometheus Stack”. One Core worker thread (not Celery). **NetBox** UI up with API 403 is **warn** (yellow), not paused — Core is a token on the NetBox superuser, not a UI login. **SNMP exporter** with no Network device + IP is **paused (no SNMP targets)** (yellow, not down). Do not add devices just to un-pause SNMP. |
| Email & reports | `/ops` | Address book, send, **the** mail outbox (`#mail`), scheduled reports with **Edit / Clone / Remove / Enable**. Grafana is on System Health. |
| Administration | `/admin` | Users: click a row to **edit** or **remove**. **Backup**, then **Import / restore** (left) beside a **ForgeSRE CLI** command list (right). Audit log. No browser PTY — SSH or `./forgesre` / `./forgesre shell` |

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

Audit rows for `user.create`, `user.update`, `user.delete`, `backup.create` / `backup.download` / `backup.import` / `backup.restore`, and `login` show on `/admin`.

### Platform backup and restore

Archives are `data/backups/backup_YYYYMMDDTHHMMSSZ/forgesre.tar.gz` (`$FORGESRE_DATA/backups/`; one folder per run, plus `MANIFEST.txt`). The directory is gitignored, mode `700`; each tar is mode `600`. Administration (admin / super_admin only) has **Backup** and **Import / restore** on the left, with a **ForgeSRE CLI** cheatsheet on the right. `./forgesre backup` writes the same archive; `./forgesre update` also backups first as a safety net. GUI/Core uses SQLAlchemy; the host CLI dumps Postgres with `docker compose exec postgres` (do not pip-install sqlalchemy on the VM). Host `./forgesre verify` likewise must not import sqlalchemy.

**In the archive:** `config/forgesre.yml`, `.env`, `secrets/secrets.env` (omit with `./forgesre backup --no-secrets`), a logical database dump (users, incidents, assets, playbooks, playrules, journal, audit, jobs), compressed `data/logs/`, `monitoring/alerts.local.yml` if present, `data/generated/`, `config/examples/`.

**Not in the archive:** Docker images, Prometheus/Loki/Grafana volume data, nested backups, optional mailbox mail. LLM GGUF files under `data/models/` are skipped unless you tick **Include LLM GGUF** or pass `--include-models` (they are often multi-GB). Small non-GGUF files in that folder are included.

Download is session-authenticated. There is no unauthenticated URL. Do not commit `data/backups/`.

Restore does **not** run silently. CLI without `--yes` prints the plan and exits 1. The UI Restore button requires a checkbox and typing `RESTORE`. Preferred path after a crash or when migrating VMs:

```bash
ssh you@forgesre-vm
cd ~/forgesre
docker compose stop core
./forgesre restore data/backups/backup_YYYYMMDDTHHMMSSZ --yes
./forgesre update
```

A browser restore can reload Postgres while Core is still running; it cannot rewrite `.env` / secrets / YAML because those mounts are read-only. Finish file restore from SSH as above. Then `git pull origin main && ./forgesre update` if you are also taking new code.

### ForgeSRE CLI (no web PTY)

Administration does **not** open a terminal in the browser. A full web PTY (xterm.js + host PTY), even if wrapped to `./forgesre` only, is still a large attack surface: XSS or a stolen admin cookie becomes a host command channel, and “restricted shells” are routinely escaped. ForgeSRE will not ship that, and will not expose root bash in the UI.

The Administration page keeps **Import / restore** on the left and a scannable `./forgesre` command list on the right (`./forgesre help` is the source of truth). One line in the UI: no browser terminal — SSH, then `./forgesre` or `./forgesre shell`.

```bash
ssh you@forgesre-vm
cd ~/forgesre
./forgesre          # or: ./forgesre shell
./forgesre help
```

`./forgesre` with no args is already a restricted prompt (`forgesre>`). Use it on the box, not through the browser. Journal (`/journal`) is the process console, not a shell.

Residual risk of SSH + `./forgesre`: whoever has a Linux account on the VM can run the CLI (and the install admin fallback in `secrets.env` if that file is readable). Treat OS accounts as you would any appliance login. Do not put the UI on the public internet.

---

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

A row on **Assets** is what ForgeSRE calls a server (or switch, or appliance). You can add one **manually** or via **Discovery** (Approve). Prometheus does **not** scan the network. The first column **#** is a stable asset number (auto-increment; deleting a host does not renumber the rest). Search and `./forgesre verify 12` use that number — it is not a volatile row index.

- **Linux:** after the host is in inventory with `scrape_address=<ip>:9100`, Prometheus HTTP SD scrapes **node_exporter**.
- **Windows:** after the host is in inventory with type `Windows Server` and `scrape_address=<ip>:9182`, the same HTTP SD scrapes **windows_exporter**. ICMP ping is not a scrape.
- **Network device:** after the row has type `Network device` (or switch/router/firewall) **and an IP**, bundled **snmp_exporter** walks **UDP/161**. The scrape address stays empty on purpose (no exporter `up == 0` noise).

NetBox is **bundled and on by default** (`http://<VM-IP>:8001`). Core still uses local inventory as the monitoring source of truth; NetBox is a read-sync.

### A. Manual (you already know hostname + IP)

Who: **analyst**, engineer, or admin (`write_assets`).

1. **Assets** → **Add asset**.
2. **Asset ID** first (required short slug, e.g. `win10-gp`; lowercase letters, digits, hyphens — not derived from hostname), then **Hostname** (OS name, e.g. `DESKTOP-CG81N3J`), then IP, then the rest: type (**Auto (detect exporter)** is the default — or `Linux Server` / `Windows Server` / `Network device` / `Web/appliance`), environment, owner/team, **contact name, owner email, owner phone**, optional **scrape address** (`ip:9100` or `ip:9182`), notes. No extra ticketing / Zabbix / IMAP fields — those are not used.
3. **Save**. You land on the asset page (a banner explains what detect found).

**Edit / Clone / Verify / Remove** are on the list (and the asset page). Same permission as Add (`write_assets`: analyst, engineer, admin). Viewers only read.

- **Edit** opens the same Add form filled in (`/assets?edit=<id>`). **Asset ID is immutable** after create (Prometheus `asset=` label and history). Hostname, type (including Auto), IP, scrape address, owner/contact, notes, environment can change. HTTP SD is live from this table — the next Prometheus scrape drops or rewrites the target. Core’s static demo job is not this list.
- **Clone** copies into the same form with a **new** Asset ID (and a suggested hostname). Tweak before Save. Duplicate Asset ID or IP is rejected. NetBox id is not copied. If the source is `forge-demo-*`, the suggested id is `copy-…` (a real asset that **can** be scraped). Keep a `forge-demo-*` id only if you want another lab-only row.
- **Remove** asks for confirm. The row leaves inventory and HTTP/SNMP SD. **Incidents stay** in History with the asset link cleared (not cascade-deleted). Discovery candidates for that IP go back to **new**. Lab `forge-demo-*` hosts can be removed the same way; seed will not put them back after Core start/update.
- **Verify** runs the live path for that row (same as `./forgesre verify 12` / `win10-gp` / hostname / IP): ping, exporter port or SNMP, Prometheus `up`, scrape target health, family series (`node_` / `windows_` / SNMP), Alertmanager reachable, last Core incident (SKIP if none), last RCA vs PromQL. ForgeAI is listed only if enabled; verify does not call the LLM. If scrape address is empty and type is Unknown/Auto, verify still GETs `:9100` and `:9182` on the IP (same as Add Auto). `node_` → Linux `:9100`, `windows_` → Windows `:9182`, and the row is saved so Prometheus HTTP SD can scrape it (wait ~30s, then `./forgesre sd`). Missing exporter = SKIP/FAIL with a reason, not a fake green host. Demo `forge-demo-*` and discovery seed `10.20.30.41` (`disc-10-20-30-41`) are labeled lab and are not scraped. **Verify all** is on the Assets list. **Verify ≠ `./forgesre doctor` (System Health) ≠ `./forgesre test` (appliance report).** GUI Verify ICMP runs from the **Core container** (image includes `iputils-ping`). Host CLI `./forgesre ping` / `verify` still probe from the Ubuntu VM.

What Core does:

- `asset_id` is a **short unique slug the operator types at Add** (`win10-gp`), not derived from hostname (`DESKTOP-CG81N3J`). Unique, lowercase letters / digits / hyphens. Edit cannot change it (Prometheus `asset=` label and history). Hostname and IP can change.
- **Auto (default):** Core GETs `http://<ip>:9182/metrics` and `http://<ip>:9100/metrics` from this appliance. It reads far enough to see `node_` / `windows_` (those families often sit after `go_*` runtime metrics) and then **drains** the rest of the body so node_exporter does not log `broken pipe` / `error encoding and sending metric family`. A 4 KiB abort used to miss `node_` and leave type Unknown with an empty scrape.
  - `windows_exporter` / `windows_` metrics → `Windows Server`, `monitoring_profile=windows-standard`, `scrape_address=<ip>:9182`.
  - `node_exporter` / `node_` metrics (`node_uname` / `node_cpu`) → `Linux Server`, `linux-standard`, `<ip>:9100`.
  - **Both:** keep a saved Linux/Windows type if the row already has one; otherwise prefer Windows `:9182` (mis-classifying Windows as Linux was the scrape miss). Override the type if the host is actually Linux.
  - **Neither HTTP family:** not guessed as Network. If SNMP UDP/161 already answered (Discovery GET, or the same probe on Add Asset Auto), type `Network device`, empty scrape, `network-switch`. ICMP miss is **not** a scrape and **not** a network fingerprint.
  - Saved Linux/Windows is not rewritten to Network by SNMP.
- Explicit **Linux Server** still defaults to `:9100` without a live probe. Explicit **Windows Server** still defaults to `:9182`.
- Network devices get `network-switch`, an **empty** scrape address, and an SNMP SD target (UDP/161 via snmp_exporter).

The Assets table shows **Ping** and **:9100 / :9182 / SNMP** color dots after the IP. They are last-known (or yellow until the first probe). The list page is not blocked; a background `GET /api/v1/assets/reachability` refreshes them (`./forgesre ping` / `asset_probe`).

- **green** — last probe succeeded (ICMP reply, or exporter `/metrics` / SNMP GET).
- **yellow** — not probed yet, skipped, or unknown.
- **red** — last probe failed (no ICMP reply, exporter down, or SNMP no reply).
- Web/appliance rows are inventory only until you set a scrape address yourself. They are **not** SNMP-scraped.
- `source=manual`.
- If owner email is set, new incidents notify that address (see §11).

On the asset page, **Detect OS / scrape port** re-runs the same probe and fills type + scrape. You can override afterwards.

The same page has a **right-hand Machine metrics** panel (two columns; stacks on a narrow window). Each metric is **one line** (name · value · bar · color · threshold) so a high % does not clip the threshold. Glance at bundled class metrics from Prometheus: Linux `node_` CPU/mem/disk, Windows `windows_` the same idea, network SNMP `up` only. Tiles match the same way verify does (`up{asset="<id>"}` first, then hostname, then `instance=<scrape>` such as `IP:9182`). Missing series stay **yellow** (`not collecting`) — never a fake 0%. When verify PROM is PASS, Collecting is green. Colors follow this asset’s Add/Edit checklist (else bundled alerts/playrules: Linux CPU **95%**, Linux/Windows memory and disk **90%**, Windows CPU **90%**, demo gauges **80%**). **One Edit Alarms** on the panel head opens that host’s Alarms (not an Edit on every tile). `forge-demo-*` rows and discovery seed `10.20.30.41` are labeled **DEMO**. Grafana is unchanged (graphs only; not the alarm path).

On **Add asset / Edit** (not a third column on the Assets **list**), the form is **Asset ID | Hostname | IP** as the left identity block (same order as the Assets table, skipping `#`), with **Alarms** still the third column beside them. Keep type **Auto (detect exporter)**. The Alarms checklist (cpu / mem / disk / up, enable + threshold %) sits in the third column, not a full-width block below. Detect also shows which bundled families the exporter actually exposes. Stored on the asset (`alarms` JSON). **Save** and **Cancel** are at the bottom (Cancel returns to the asset page or the list). Tiles use that threshold for red/green. Prometheus alert rules stay global — ForgeSRE skips opening an incident for a bundled alert when the alarm is disabled or the webhook value is below the asset threshold. Until you change `monitoring/alerts.yml` / `alerts.local.yml`, Prometheus may still fire; the incident list stays quiet.

API: `POST /api/v1/assets` with JSON `hostname`, optional `asset_id` (else a slug from hostname for API/discovery), `ip`, `type` (`Auto (detect exporter)` to probe), `environment`, `owner`, `contact_name`, `owner_email`, `owner_phone`, `notes`, optional `scrape_address`. Detect-only: `GET /api/v1/detect-exporter?ip=`. List ping/exporter colors: `GET /api/v1/assets/reachability` (async; the HTML list is last-known). Verify live path: `GET /api/v1/assets/{id}/verify` and `GET /api/v1/verify` (`write_assets`) — empty scrape + Unknown still probes `:9100`/`:9182` and **saves** type + scrape when `/metrics` has `node_` / `windows_`. Glance tiles: `GET /api/v1/assets/{id}/metrics` (`read_assets`). Update: `POST /api/v1/assets/{asset_id}` (hostname, type, IP, scrape, contacts; id stays). Clone: `POST /api/v1/assets/{id}/clone`. Delete: `POST /api/v1/assets/{id}/delete`.

Similar-incident history on the asset page groups past incidents by alert/title (count, open count, last seen). Seed already puts a closed HighCPU on `forge-demo-01` so this is visible after install.

### B. Discovery (scan the management network)

Who: **analyst**, engineer, or admin to scan and Approve. After Approve, fill contacts on the asset page — discovery does not guess who owns the box. You can still add **manual Assets** on `/assets` without ever running discovery — inventory stays the monitoring source of truth.

1. Open **Discovery**. Scan targets are the **union** (deduped) of:
   - `discovery.cidrs` from live `config/forgesre.yml` (always honored when present)
   - **Auto-detected** connected IPv4 nets on this appliance (real `prefixlen` — never a hardcoded `/24`). Skips loopback, link-local, multicast, `0.0.0.0/0`, and Docker bridges (`docker0`, `br-*`, `veth*`).
2. Empty YAML → auto only. Auto-detect fails → YAML only. Both present → merge. **Save & scan** writes the form into `discovery.cidrs`; **Scan now** always merges auto-detect. Edit YAML by hand if you prefer — no `install.sh` needed.

```yaml
discovery:
  enabled: true
  mode: semi-automatic   # manual | semi-automatic | automatic
  cidrs: ["10.20.30.0/24"]   # optional extras; [] still scans auto-detected nets
```

3. **Scan now** is the TCP/SNMP/`/metrics` probe — **not nmap**, and **not** NetBox. Limits: **256 hosts per CIDR**, **1024 total**. **Sync NetBox** is a separate admin CTA (read-only), shown **side-by-side** with Scan now. Background loop uses the same YAML ∪ auto sources. Hosts already in Assets are skipped. Results land in the candidate table (10 per page) and in Journal module `discovery`. ICMP ping is on Assets, not Scan now.
4. Banner **NEW DEVICE DETECTED**. Found hosts stay on **Waiting for Approve** until you decide. They are **not** auto-added to inventory.
5. Candidate table shows **Open ports**, **node_exporter**, **windows_exporter**, and **SNMP** (`snmp_ok`).
6. **Approve** (on the Discovery page) → creates an asset (`source=discovery`, id like `disc-10-20-30-41`) and sends you to the asset page. **Ignore** rejects the host (out of inventory).


### C. Bundled NetBox (read-sync)

`docker compose up -d` / `./forgesre update` starts NetBox with **no compose profile**. Image pin: `netboxcommunity/netbox:v4.6.9-5.0.2` (the old `v4.4-3.2.0` tag is not on Docker Hub). UI: `http://<VM-IP>:8001`. First login: user `admin`, email `admin@forgesre.local`, password `NETBOX_SUPERUSER_PASSWORD` in `secrets/secrets.env` (not an operator personal email). First boot pulls a large image then runs Django migrations and can take several minutes — **System Health** / `./forgesre doctor` stays **yellow / starting** until `http://127.0.0.1:8001/login/` answers. That is not a fake green.

**Core is not a NetBox UI login.** `NETBOX_API_TOKEN` is a read-only token on the Django **superuser** (`NETBOX_SUPERUSER_NAME`, usually `admin`) with `is_superuser` and `dcim | device | Can view device`. ForgeSRE does not create a separate NetBox user named Core. A valid token on a user **without** that permission is still HTTP **403** on `GET /api/dcim/devices/`. Prefer a NetBox UI **v2** token in secrets (see below). A legacy 40-character **v1** value is still upserted on every NetBox start (`scripts/netbox-upsert-token.py`). `docker compose logs netbox | grep forgesre` shows **`v2 token` / `skipping v1 upsert`** when secrets already hold `nbt_…`, or **`v1 token ready`** for the fallback. **`could not upsert`** means a v1 secret never reached the NetBox DB (UI still starts).

On **System Health** (`/health-ui`) the NetBox tile follows the Discovery traffic light, not “paused”: **starting** (yellow) while first-boot migrations run; **warn** (yellow) when the UI answers but the devices API is 403 / token empty; **running** (green) when the API is 200. A bundled NetBox UI that is up must not show **paused**. **SNMP exporter** is a different tile: with **no** Network device + IP it is **paused (no SNMP targets)** — that is idle, not broken, and you do not need to add devices to un-pause it.

Core finds it via compose env `NETBOX_URL=http://127.0.0.1:8001` and `NETBOX_API_TOKEN` from `secrets/secrets.env` (Core reads the bind-mounted secrets file; do not interpolate an empty project `.env` over `env_file`). Discovery only mentions `--netbox-url` / an external instance when `inventory.netbox.url` is **not** localhost. When `NETBOX_API_TOKEN` is still a **plain** 40-character **v1** value, launch upserts it (`plaintext` column, `write_enabled=False`, on `NETBOX_SUPERUSER_NAME`, permission to list devices) via `scripts/netbox-upsert-token.py` — success log **`v1 token ready`**. When the secret looks like **v2** (`nbt_…`), launch **skips** that upsert (`skipping v1 upsert`) and Core sends `Authorization: Bearer`. **`could not upsert`** (UI still starts) means a v1 secret **never landed in the NetBox DB** — Discovery stays HTTP 403 and that is honest. The helper logs the Python exception type and message (never the token). NetBox 4.5+ has no `User.is_staff`; touching it was why upsert failed on a live v4.6 database. NetBox v4.6 defaults to hashed **v2** tokens (`key` is a 12-character public id + HMAC digest); writing the secret into `key` is why `GET /api/dcim/devices/` stayed HTTP **403**. netbox-docker 5.0.2 also skips `SUPERUSER_API_TOKEN` unless `SUPERUSER_API_KEY` is set, and first-insert-only superuser creation does not help an existing database. After `git pull origin main && ./forgesre update` (recreates `netbox` so launch runs), Core sync is GET-only. On **Discovery**, the NetBox light is a non-clickable status chip (`role=status`, CSS color, not a `<button>`; **Sync NetBox** is the only action) from `GET /api/dcim/devices/` (not a write): **grey** **Not connected** / **API 403** (UI down, API 403, no token); **yellow** **No devices** (API 200 and count == 0; empty is normal). Sentence: *No devices yet; add in NetBox UI `:8001` or use Assets/Discovery.* **green** **Connected** (API 200 and count ≥ 1). Empty must not look like 403. HTTP 200 with a v2 token is yellow or green — do not recreate a v1 token. HTTP 403 with `API token: yes` means NetBox rejected the token Core already has (often a 12-character key instead of the full `nbt_…` secret, or core not recreated after `secrets.env`) — recreate **core** for v2, **netbox+core** for v1; `API token: no` means the token is empty. Admin **Sync NetBox** is clickable on yellow and green; grey still allows a retry click when the UI answers (`/login/` / `/api/status/`) — a devices 403 is a warning, not `disabled`. First-boot (UI not answering) stays disabled with one sentence. Engineers see a disabled CTA and **Admin only.** Devices become local assets (`source=netbox`). Core **never writes** back to NetBox. Local inventory stays the monitoring source of truth. Do **not** re-run `./install.sh` (that regenerates secrets).

**Prefer a NetBox UI v2 token.** NetBox 4.6 deprecates v1 in the UI; that is OK. Create a token at `:8001`, copy the **full** secret shown **once** (`nbt_<key>.<secret>` — not the 12-character key in the list), put it in `NETBOX_API_TOKEN` in `secrets/secrets.env`, then recreate **core**. Core sends `Authorization: Bearer`. Launch **skips** the v1 upsert when that secret looks like v2 and will not overwrite it. A 40-character v1 value is still upserted as fallback (`write_enabled=False` on `NETBOX_SUPERUSER_NAME`). Do not create a second Core user in NetBox. v2 hashing still needs `API_TOKEN_PEPPERS` (image env `API_TOKEN_PEPPER_1`). `./forgesre update` writes `NETBOX_API_TOKEN_PEPPER` once into `secrets/secrets.env` (and `.env`) if it is missing. `SECRET_KEY` for NetBox is `NETBOX_SECRET_KEY` (already generated). Do not drop database `forgesre`. Never print the token. Do not re-run `./install.sh`.

To disable the Core sync (container can keep running): `inventory.netbox.mode: disabled`. To use only an existing DC NetBox: `mode: external` and the URL/token.

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

Each asset with a non-empty `scrape_address` becomes a target, labeled with `asset=<asset_id>` and `job=<monitoring_profile>`. Seeded `forge-demo-*` hosts and the discovery Approve seed `10.20.30.41` are lab-only and are **not** in this list.

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
./forgesre verify              # live path through Prometheus (not ./forgesre test)
./forgesre verify win-01
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
- **node_exporter** (`NodeExporterDown`, `NodeFilesystemUsageHigh`, `NodeCPUHigh`, `NodeMemoryHigh`) for HTTP SD targets with `job=linux-standard`
- **windows_exporter** (`WindowsExporterDown`, `WindowsFilesystemUsageHigh`, `WindowsCPUHigh`, `WindowsMemoryHigh`) for HTTP SD targets with `job=windows-standard`
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

A **playrule** maps `alertname` → playbook + severity. AI cannot edit playrules. Creating a playrule does **not** create a Prometheus alert.

Who: **analyst** (permission `write_play`). Engineers can read, not create.

`monitoring/alerts.yml` is the **PromQL source** (bundled rules). Asset **Alarms** on Add/Edit (`assets.alarms` enable/%) are a per-host overlay: same Alertmanager webhook, not a second engine. Prometheus may still fire a global rule; ForgeSRE skips opening an incident when the alarm is disabled or the value is below the host threshold.

### Create in the UI

1. Optionally create the playbook first (`/playbooks`).
2. **Playrules** → **Create playrule**.
3. Name (unique), **alertname** (must match Prometheus), metric/operator/value for humans, severity, playbook.
4. **Save**. **Cancel** next to Save returns to this page without creating. Use **Toggle** to disable without deleting.

The form stores `condition.alertname`. Matching on ingest is:

1. Enabled playrule whose `condition.alertname` equals Prometheus `labels.alertname` (case-insensitive), else
2. `condition.metric` equals a `metric` label or the alert name.

So if Prometheus fires `alertname: HighCPU`, the seeded rule `high-cpu` matches because its condition includes `"alertname": "HighCPU"`. Extra Prom rules: `monitoring/alerts.local.yml`, then `./forgesre render-monitoring`.

### Seeded rules

| Playrule | Matches alert | Playbook |
|---|---|---|
| `high-cpu` | `HighCPU` | `CPU-HIGH` |
| `high-disk` | `FilesystemUsageHigh` | `DISK-FULL` |
| `snmp-down` | `SnmpDeviceUnreachable` | `NETWORK-UNREACHABLE` |
| `node-exporter-down` | `NodeExporterDown` | `HOST-UNREACHABLE` |
| `node-filesystem` | `NodeFilesystemUsageHigh` | `DISK-FULL` |
| `node-cpu` | `NodeCPUHigh` | `CPU-HIGH` |
| `node-memory` | `NodeMemoryHigh` | `MEMORY-HIGH` |
| `windows-cpu` | `WindowsCPUHigh` | `CPU-HIGH` |
| `windows-filesystem` | `WindowsFilesystemUsageHigh` | `DISK-FULL` |
| `windows-memory` | `WindowsMemoryHigh` | `MEMORY-HIGH` |
| `windows-exporter-down` | `WindowsExporterDown` | `WINDOWS-UNREACHABLE` |

API: `POST /api/v1/playrules` with `name`, `condition` (object), `playbook_id`, `severity`.

---

## 10. Playbooks

A **playbook** is a checklist shown on the incident. V0.3 **does not execute commands**. No SSH, no scripts, no auto-remediation.

Who: **analyst** to create.

1. **Playbooks** → **Create playbook**.
2. Name (display, e.g. `DISK-FULL`), slug (unique id, e.g. `disk-full`).
3. Steps: **one title per line**.
4. **Save**. **Cancel** next to Save returns to this page without creating.

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

Incident reports and escalation mail are **multipart** (`text/plain` + `text/html`). Gmail and Outlook show the HTML (severity color bar, DEMO banner when the asset is `forge-demo-*`, ForgeRCA sections). The **mail outbox** on `/ops#mail` stores the plain-text body. **Send email** on `/ops` (Compose) stays as the operator typed it — ForgeSRE does not rewrite that text into HTML tables.

Analysts can **create** another policy (name, slug, steps as `minutes role` lines) with **Save** and **Cancel**, same pattern as Playbooks. New playrules still attach **Default warning** unless the playrule form picks a different policy. The 30s loop reads that policy’s `after_minutes` / `target` steps. This is not a ticket system.

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

### Own domain (not enabled)

Compose profile **`mailbox`** (Postfix + Dovecot + Roundcube `:8081`) is off at install. `./forgesre mailbox` starts it later and does **not** rewrite Core SMTP unless you pass `--bind-core`. Receive still needs MX and **TCP/25** (often blocked). There is no lab SMTP catcher container. Leave YAML email **disabled** for an on-box outbox (`generated`), or send through Gmail / Outlook.

### Scheduled reports (`/ops#reports`)

Rows are Postgres `scheduled_reports` (not Celery, not YAML). Analyst / engineer / admin can **Edit** (same create form; **Save** updates that row; **Cancel** next to Save discards and returns to the list), **Clone** (draft copy until Save), and **Remove** (confirm; outbox mail already generated stays). **Enabled** means the job fires at **Next**; **Disable** keeps the row stored and the scheduler skips it. Send now and incident **Send report** are unchanged.

---

## 12. Incident workflow

On `/incidents/<number>`:

| Button | Who | Effect |
|---|---|---|
| Acknowledge | analyst+ | Status `INVESTIGATING`, records ack user/time |
| Resolve / Close | analyst+ (`write_incidents`) | Closes the operational loop; records who resolved |
| Open ForgeRCA | analyst+ (`read_ai`) or engineer (`investigate`) | Primary CTA to `/ai/INC-…`. Runs builtin ForgeRCA if needed; does not change the host |
| **Send incident report** | analyst / engineer / admin | Emails the current INC snapshot when SMTP is on (`sent`) as HTML + plain text. If SMTP is off, stores `generated` in the **mail outbox** on `/ops#mail`. Replies arrive in the real mailbox, not in ForgeSRE |

The same page lists **who did what** (audit: ack, resolve, notes) and **operator notes**. Mail bodies are on `/ops#mail`, not a second table here. Notes are not a ticket thread and not RCA.

**Incidents** (`/incidents`) is open/firing work (filter: open-only / last days). **History** (`/history`) is the archive. Escalation is still the policy; its outbox is `/ops#mail`. Console (`/journal`) is still process reports.

Asset health on the dashboard (`healthy` / `warning` / `critical`) follows open incidents on that asset.

**Grafana is not the alarm path.** Incidents come from Prometheus → Alertmanager → Core. Open Grafana only from **System Health**. Grafana is not in the left nav. If Grafana is down, doctor stays **yellow** (warn) — that is not a Prometheus outage and must not write a Journal error. A real Prom or Alertmanager failure journals `core` / `doctor` **error** naming the hop (`Prometheus :9090`, `Alertmanager :9093`), never a vague “Prometheus Stack”.

---

## 13. AI investigation (ForgeRCA)

Open **ForgeRCA** from the incident (primary CTA → `/ai/INC-…`).

The button runs **builtin ForgeRCA immediately** and opens Summary → Root cause → Recommended actions → Facts → Anomalies → Candidate causes → Limitations. Two pills sit at the top: **ForgeRCA** (green when builtin has a result) and **ForgeAI** (green if the LLM rewrote the prose, yellow while the rewrite runs, red if the LLM is off or unreachable). If `ai.enabled` is on, refresh later for ForgeAI. Do not mash Run now.

Jobs: **one worker thread** in Core (Postgres `jobs` table). There is **no Celery**. An LLM rewrite can occupy that thread up to `ai.llm.timeout_seconds` (example.yml default 90; live yml is gitignored). Scheduled `/ops` reports run first in the same loop.

You get:

- Facts vs hypotheses vs anomalies
- Evidence IDs (`EV-…`) with PromQL/LogQL for engineers
- A **ForgeSRE confidence score** (not the LLM’s own number)
- Disclaimer: `AI has not modified the system.`

RCA works with `ai.enabled: false` (builtin analyst). To use a local LLM, download the GGUF (not in git) with `./forgesre fetch-llm`. Implementation (hardware, Compose profile `ai`, offline GGUF, jobs, debug): [`llm.md`](llm.md).

Alertmanager ingest **enqueues** an investigate job. The webhook does not wait on the LLM. `./forgesre jobs` lists pending / running / done / error. Demo (`./forgesre demo`) still runs RCA inline so the first-hour path is immediate.

Queries are **per asset**. `forge-demo-01` uses Core demo gauges. A real Linux host uses `node_cpu_seconds_total` / `node_filesystem_*` with `asset="<id>"`. A real Windows host uses `windows_cpu_time_total` / `windows_logical_disk_*`. A network device uses `up{job="forgesre-snmp",asset="<id>"}`. Demo CPU/disk numbers are never overlaid on another host.

Loki: Alloy still only ships **appliance Core logs** labeled `asset=forge-demo-01` / `job=forgesre` as a demo. RCA may use those for the DEMO host, labeled DEMO. **Real inventory hosts have no Loki until later** — limitation is “no host logs shipped”. Empty Loki is not evidence from that VM. There is no second log stack in V0.7. The RCA page shows that one-liner when the limitation is present.

The optional LLM **rewrites prose only**. It receives a compact context (incident title/severity, top facts, CPU/mem/disk snapshots, short log lines already capped by `max_log_lines`) — not Prometheus `values: [[timestamp, x], …]` matrices. Builtin ForgeRCA still stores the full facts, evidence IDs, and PromQL. A CPU 4B `cancel task` at `timeout_seconds: 300` was prefill of a multi-thousand-token dump, not Node Exporter talking to the model. After `git pull origin main && ./forgesre update`, Investigate again and watch `docker compose logs -f llm` — prompt tokens should drop a lot.

Lab: `./forgesre demo-rca` raises filesystem usage on the **demo gauge** (does not fill a real disk). `./forgesre demo-reset` puts the gauges back.

---

## 14. Worked example: onboard a Linux server

Goal: host `app-01` at `10.10.10.50` appears under Assets and is scraped on `:9100`.

1. On `app-01`, run node_exporter listening on `0.0.0.0:9100` (or at least on the management NIC). From the ForgeSRE VM: `./forgesre ping app-01` (or `curl -fsS http://10.10.10.50:9100/metrics | head`). ICMP ping alone is not a scrape.
2. Sign in as analyst/engineer/admin. **Assets** → hostname `app-01`, IP `10.10.10.50`, leave type **Auto (detect exporter)** (or pick `Linux Server`), owner email/phone of who to call → **Save**.
3. Asset page should show scrape address `10.10.10.50:9100` and the contacts. Edit them later if the owner changes.
4. Wait up to 30s, then check SD JSON (command in §7) contains that target.
5. On the VM: open Grafana (`:3000`) or Prometheus UI (`http://127.0.0.1:9090` from the host) and query `{asset="app-01"}` or `up{instance="10.10.10.50:9100"}`.

Optional discovery path: Confirm `10.10.10.0/24` on Discovery (or put it in `discovery.cidrs`), Scan now, Approve the `10.10.10.50` candidate instead of the manual form.

This still will **not** open `INC-…` until a Prometheus alert fires with a matching playrule. Bundled `NodeExporterDown` / `NodeFilesystemUsageHigh` / `NodeCPUHigh` / `NodeMemoryHigh` already match seeded playrules once node_exporter is scraped. Custom thresholds: §15.

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

Bundled `WindowsExporterDown` / `WindowsFilesystemUsageHigh` / `WindowsCPUHigh` / `WindowsMemoryHigh` match seeded playrules `windows-exporter-down` / `windows-filesystem` / `windows-cpu` / `windows-memory`.

### Network switch (SNMP)

Goal: `core-sw-01` at `10.30.1.1` is walked by snmp_exporter.

1. On the switch, enable SNMPv2 read-only with a community the ForgeSRE VM may use. ACL: allow the ForgeSRE host on **UDP/161**.
2. From the ForgeSRE VM: `./forgesre doctor` should show `snmp` **running** once this switch (or any Network device + IP) is in inventory. With **no** SNMP targets, doctor / System Health shows **paused (no SNMP targets)** (yellow) — that is not DOWN and not broken. Do not add a dummy switch just to un-pause the tile. If it is down with real network assets: `docker compose up -d snmp-exporter` (bundled compose; not Zabbix).
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

**Commands:** [`cli.md`](cli.md). `./forgesre help` / `./forgesre help <command>` on the VM. This section is **when to use which**.

| When | Command |
|---|---|
| Live box after `git pull` | `./forgesre update` — never `./install.sh` |
| Stack lights (same as `/health-ui`) | `./forgesre doctor` |
| Appliance report → `data/reports/` | `./forgesre test` |
| Live inventory path (exporter → Prom → AM → Core) | `./forgesre verify` / `./forgesre ping` |
| SNMP targets JSON | `./forgesre snmp` |
| RCA / LLM queue | `./forgesre jobs` (one worker thread, no Celery) |
| Safety copy | `./forgesre backup` |

`./forgesre` with no extra words opens `forgesre>`. Type `journal`, `incidents`, `doctor` — not `./forgesre` again. Leave with `quit`. Host CLI must not import sqlalchemy (do not `pip install sqlalchemy` on the VM).

ForgeSRE does **not** speak SSH of its own. SSH to Ubuntu, then run the CLI on localhost. Two logins: Linux account vs ForgeSRE user (`./forgesre login` → `data/cli.session`). Without that cookie, the CLI uses the install admin from `secrets.env` if readable.

API paths (session cookie after `/login`, except webhooks/SD which use the bearer token): [`cli.md`](cli.md) § API.

Install/config files: [`install-config.md`](install-config.md). Do not commit `.env`, `secrets/secrets.env`, or `data/`.

**Console** (`/journal`) is the internal process journal: seed, inventory, discovery, snmp, incidents, RCA, notifications, demo, install. Each action writes a short ok/warn/error report. Rows are split by module and pruned automatically (~200 per module) so search stays small. This is not a dump of Docker logs and not the Administration audit log (who clicked what). Prometheus HTTP SD is **not** journaled on every scrape (that would flood the table). Doctor journals `core` / `doctor` only when Prometheus or Alertmanager is actually down, naming the hop (`:9090`, `:9093`) — Grafana down does not write that error.

Root wrappers `./doctor.sh`, `./test.sh`, `./backup.sh`, `./update.sh`, `./install.sh` still work; they call the same scripts as `./forgesre`.

---

## 17. What this version does not do yet

Say this out loud so lab expectations stay honest:

- No Kubernetes, no APM, no tracing, no auto-remediation.
- Playbooks are checklists, not executed runbooks.
- Assets can be added, edited, cloned, and removed (analyst+). Lab `forge-demo-*` rows can be removed; they do not come back on update. Users can be edited and removed on Administration (not the install super admin, not yourself). Escalation policies can be created in the UI; there is no ticketing object.
- Example YAML in `config/examples/` is not applied automatically.
- Bundled alert rules include demo gauges, SNMP `up` / interface-down, Linux `node_exporter` (down / disk **90%** / **memory 90%** / CPU 95%), and Windows `windows_exporter` (down / volume **90%** / **memory 90%** / CPU 90%). Extra rules go in `alerts.local.yml`. **Grafana is not the alarm path** — it is graphs only. Incidents come from Prometheus → Alertmanager → ForgeSRE.
- Discovery is TCP 22/80/443/9100/9182 plus SNMP GET on UDP/161, 256 hosts max. It does not use TCP/161. SNMP *polling* is still snmp_exporter after Approve.
- Viewer cannot open Playrules, Playbooks, Escalation, Console, or Discovery (403).
- Optional TLS is an example Caddyfile, not a default container.
- NetBox is read-only. Bundled UI is on by default; Core never writes back.
- Re-running `./install.sh` regenerates secrets. Core will not start on shipped default `SECRET_KEY` / webhook token (`FORGESRE_DEV=1` is tests/lab only).

When that is enough: install ([`install-config.md`](install-config.md)), add people (§5), add servers (§6–7), then add real alerts only when you are ready for incidents (§15). First-hour lab path: Dashboard **Run demo** → HighCPU on `forge-demo-01` (DEMO pill) → Who to call / Escalation. CLI: `./forgesre demo`.
