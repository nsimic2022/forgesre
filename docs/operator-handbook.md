# ForgeSRE operations handbook

How to **operate** ForgeSRE after it is installed: users, servers, monitoring, playrules, playbooks, incidents, email, and RCA.

Install, host packages, `./forgesre test`, and configuration files: [`install-config.md`](install-config.md).  
CLI reference (everyday + advanced): [`cli.md`](cli.md).  
Verification report: [`verify.md`](verify.md).  
Local LLM (ForgeAI): [`llm.md`](llm.md).

Version notes (`v0.1.md` … `v0.8.md`) explain *what shipped*. This document explains *how you run the product*. Commands live in [`cli.md`](cli.md). Learning order: [`docs/README.md`](README.md).

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
18. [Zabbix (read-only source)](#18-zabbix-read-only-source)

---

## 0. How to learn the platform

Follow this order on a live box. This handbook is **why and when**. Typed commands: [`cli.md`](cli.md).

1. **Install or update.** New VM: [`install-config.md`](install-config.md) then `./install.sh`. Live box: `git pull origin main && ./forgesre update`. Never `./install.sh` again (it regenerates passwords).
2. **Login.** `http://<VM-IP>:8080` with `installation-report.md` / `secrets/secrets.env` (§5).
3. **System Health** (`/health-ui`) = `./forgesre doctor`. Open Grafana only here. Alarm path is Prometheus → Alertmanager → Core. NetBox UI up with API 403 is yellow **warn**, not paused. SNMP with no Network device + IP is **paused (no SNMP targets)** (yellow — leave it).
4. **Assets** (`/assets`). Local inventory is the monitoring source of truth. Add / Edit / Verify.
5. **Discovery vs NetBox** (`/discovery`). Suggests this VM’s primary IPv4 `/24`; **Confirm & scan** writes `discovery.cidrs` (empty = no scan) and queues a background `discovery_scan` job (Postgres `jobs` table, one worker, **no Celery**). **Scan now** only after a confirmed CIDR. Probe: TCP 22/80/443/9100/9182 + SNMP **UDP/161** + `/metrics` (not nmap). Candidate table: open ports, node_exporter, windows_exporter, SNMP. **Sync NetBox** sits beside Scan now (read-only from bundled `:8001`). Empty NetBox is **yellow No devices** — that is OK. Approve or Ignore; Manual Assets stay SoT.
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

YAML under `config/examples/` is the **future spec** (Playrule/Playbook/Escalation as files). V0.8 does **not** import those files. Live playrules and playbooks are created in the UI (or API) and stored in Postgres.

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

Left nav is a constant dark shell (does not follow the theme). The control at the bottom cycles **Light / Dark / System**; System follows the OS for the main pane only. After a CSS change, hard-refresh so `/static/app.css` is not served from cache. Operator list tables (dashboard recent incidents, incidents, history, journal, mail outbox, scheduled reports, assets, discovery, playrules, playbooks, audit log, backups) show **10 rows per page** by default with Previous / 1 / 2 / 3 / Next on the left and a **Rows** select (10 / 20 / 50 / 100) bottom-right. The choice rides in the URL (`?per_page=20`; `reports_per_page`, `audit_per_page`, `backup_per_page` on pages with two lists), so refresh and filters keep it. Changing page or **Rows** keeps the window where it was (no jump to the top). Every one of these lists (and the playbook / escalation policy cards) works like Dashboard Recent incidents: it scrolls inside its own box with a sticky header, sized to about 10 rows, so Rows 50 scrolls the list and not the page. A row click only selects (highlights) the row — it never navigates; open the record with its identity link (incident title or short number, Asset ID, user email, Similar history **Last**, mail **View**), or press Enter on the selected row. Up / Down, j / k, Home / End move the selection inside the box; inputs keep their keys, Tab leaves the list, and a click on empty page space hands scrolling back to the page. Junk values fall back to 10; anything above 100 is capped at 100. Each row has a checkbox (`name="selected"`, header box = select page); there are no bulk actions yet.

| Menu | URL | What you do there |
|---|---|---|
| Dashboard | `/` | Large **Incidents** / **Infrastructure** tiles with the **ForgeSRE** card on the right (clock, CPU / RAM / HDD for the ForgeSRE VM itself — green dash below 80 %, yellow 80–94 %, red 95 %+ or no reading; **NET** green = this VM has a default route on a non-docker interface whose link is up, red = no usable default route or that interface is down — no ping, no internet probe, so an air-gapped LAN with a gateway is green; below that, small read-only cubes for each System Health row — Core, Postgres, Prometheus, Alertmanager, Grafana, Loki, NetBox, LLM, Discovery, … — green / yellow / red from the same doctor check; click opens System Health), **Run demo** (admin, top right), then Recent incidents (no alarm banners below the tiles — host-down, discovery and journal errors live on Incidents, Discovery and Journal) beside **Host metrics** (a row click only selects that incident and redraws its host's graphs — open it with the title or short number link, or Enter on the selected row; the list scrolls inside its own box, Up / Down or j / k move the selection without scrolling the page, and a click on empty page space hands scrolling back to the Dashboard), then Recent journal (row checkboxes, `Showing 1–10 of N`, **Rows** 10/20/50/100 via `journal_per_page`, **Open full journal** below). The Incidents box pulses softly red while an active critical incident exists, slower yellow while other incidents are active, and is still when nothing is active (no pulse with reduced motion). Full doctor grid is **System Health**, not here. Every count is a link, and the number equals the list behind the click (same query): incident tiles **Open / Critical / Investigating / Escalated / Resolved** open `/incidents` filtered; asset tiles open `/assets` filtered (**Offline / unreachable** = NetBox offline or last stored ping red — probes refresh when the Assets list polls; **No owner email** = escalation has nobody to mail). |
| Assets | `/assets` | List inventory. **Add / Edit / Clone / Remove** (analyst+). One sentence: Verify ≠ doctor ≠ `./forgesre test`. |
| Asset detail | `/assets/<id>` | Contacts, scrape, similar-incident history. **One Edit Alarms** on the metrics panel (not per tile). |
| Discovery | `/discovery` | Prefills primary IPv4 `/24`; **Confirm & scan** / **Scan now** (disabled until confirmed) side-by-side; queue Postgres `discovery_scan` (no Celery; not nmap). Empty `discovery.cidrs` = no scan. **Sync NetBox** beside Scan now (read-only, admin). Columns: open ports, node_exporter, windows_exporter, SNMP. Demo IP `10.20.30.41` is a DEMO lab seed. |
| Incidents | `/incidents` | Default **All** statuses, newest first (10 per page). Filters: status (**All**, **Active (not resolved)** = Open + Investigating + Escalated, or one exact status), severity (**Critical**), last days. Title is the primary link; `#N` is colored green (resolved/closed), yellow (in progress), red (critical, not done). Older rows: **History**. |
| History | `/history` | Archive: last 90 days in Postgres. Filters: status, asset, `INC` number. Closed rows stay here. |
| Incident | `/incidents/INC-…` | **Acknowledge / Resolve / Close**, Who to call, **Open ForgeRCA** (primary CTA → `/ai/INC-…`), **Send incident report**. Mail outbox is `/ops#mail`. ForgeRCA/ForgeAI pills stay. |
| AI Investigation | `/ai/INC-…` | ForgeRCA (green) then ForgeAI (green/yellow/red). Facts, anomalies, hypotheses. Empty host logs: honest limitation (Alloy ships appliance/Core logs only). |
| Playrules | `/playrules` | Map `alertname` → playbook. Do **not** create Prom rules. `alerts.yml` is PromQL (shown read-only as **Fires when**); asset Alarms are overlay. Create / **Edit** / Toggle / **Remove** (analyst). |
| Playbooks | `/playbooks` | List steps, create (**analyst**). Guidance only — nothing is executed. |
| Escalation | `/escalation` | Seeded **Default warning**, create policy (Save + Cancel), recipient rules. Mail: `/ops#mail`. |
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

The Administration page keeps **Import / restore** on the left and a scannable `./forgesre` command list on the right — every subcommand, grouped (update and stack, health, inventory and SNMP, incidents, backup, optional services, CLI session). `./forgesre help` is the source of truth; a test keeps the page, the help overview, and TAB completion in sync. One line in the UI: no browser terminal — SSH, then `./forgesre` or `./forgesre shell`.

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
| Every UI user’s login password | PostgreSQL table `users.password_hash` | **Yes** — bcrypt hash (`backend/app/security.py`). The plaintext is never stored and never shown in Administration. |
| Install bootstrap admin | `secrets/secrets.env` (`FORGESRE_ADMIN_EMAIL` / `FORGESRE_ADMIN_PASSWORD`) and `installation-report.md` | File on disk, mode `600`. Values in that file are **plaintext** (not hashes). First Core boot seeds bcrypt into Postgres when the email row is missing (`backend/app/seed.py`). Changing `FORGESRE_ADMIN_PASSWORD` in the file after first boot does **not** update the DB — use Administration, and keep the file in sync for CLI fallback. |
| Session cookie | httponly, 12 hours | Signed with `SECRET_KEY` (also **plaintext** in `secrets/secrets.env`). |
| NetBox / Postgres / Grafana / SMTP / SNMP | Same `secrets/secrets.env` | **Plaintext** env for containers. Rotate by editing the file and recreating the affected service — see [`install-config.md` §11a](install-config.md#11a-changing-passwords). |

Templates (committed, no real secrets): [`.env.example`](../.env.example) and [`secrets/secrets.example.env`](../secrets/secrets.example.env). Live `.env` and `secrets/secrets.env` are gitignored.

`data/` (Postgres volume), `.env`, and `secrets/secrets.env` are gitignored. Do not commit them.

If you rotate the install admin in the UI, also edit `FORGESRE_ADMIN_PASSWORD` in `secrets/secrets.env` if you still use `./forgesre` commands that log in with that file.

---

## 6. Adding servers (inventory)

A row on **Assets** is what ForgeSRE calls a server (or switch, or appliance). You can add one **manually** or via **Discovery** (Approve). Prometheus does **not** scan the network. The first column **#** is a stable asset number (auto-increment; deleting a host does not renumber the rest). Search and `./forgesre verify 12` use that number — it is not a volatile row index.

- **Linux:** after the host is in inventory with `scrape_address=<ip>:9100`, Prometheus HTTP SD scrapes **node_exporter**.
- **Windows:** after the host is in inventory with type `Windows Server` and `scrape_address=<ip>:9182`, the same HTTP SD scrapes **windows_exporter**. ICMP ping is not a scrape.
- **Network family:** after the row has type `Network device`, `Switch`, `Router`, `Firewall`, `Storage`, `QNAP/NAS` or `Printer` **and an IP**, bundled **snmp_exporter** walks **UDP/161** — or the asset's own **SNMP port** (Edit → *Comms / monitoring*, e.g. `1161`; SD target becomes `ip:1161`). Any other type (even Linux) is polled by SNMP only when you type an SNMP port. The scrape address stays empty on purpose (no exporter `up == 0` noise) unless you type one. **SNMP version** (v1 / v2c / v3) and **Community** (Default = `SNMP_COMMUNITY`, or Custom) / v3 USM user sit under SNMP port — see [Per-asset SNMP version and auth](#per-asset-snmp-version-and-auth).
- **No default scrape:** only `Linux Server` (`ip:9100`) and `Windows Server` (`ip:9182`) fill a scrape address from the type. `Hypervisor`, `Web/appliance`, `Other`, the network family and Auto start empty (agentless — ping only — is fine). A typed odd port such as `1.2.3.4:9101` is kept as-is. The **IP** field is the address only; `10.0.0.5:9100` in IP is refused.

NetBox is **bundled and on by default** (`http://<VM-IP>:8001`). Core still uses local inventory as the monitoring source of truth; NetBox is a read-sync.

### A. Manual (you already know hostname + IP)

Who: **analyst**, engineer, or admin (`write_assets`).

1. **Assets** → **Add asset**.
2. **Asset ID** first (required short slug, e.g. `win10-gp`; lowercase letters, digits, hyphens — not derived from hostname), then **Hostname** (OS name, e.g. `DESKTOP-CG81N3J`), then IP, then the rest: type (**Auto (detect exporter)** is the default — or `Linux Server` / `Windows Server` / `Hypervisor` / `Network device` / `Switch` / `Router` / `Firewall` / `Storage` / `QNAP/NAS` / `Web/appliance` / `Printer` / `Other`; a saved custom type stays selectable), environment, owner/team, **contact name, owner email, owner phone** (owner and backup email are a pick from the Ops → Compose address book, or *Type a new email…*), optional **scrape address** (`host:port`), **SNMP port**, **SNMP version** and **Community** / v3 user under *Comms / monitoring*, **VLAN** (free text, e.g. `10` or `10.20`), notes. No extra ticketing / Zabbix / IMAP fields — those are not used.
3. **Save**. You land on the asset page (a banner explains what detect found).

**Edit / Clone / Verify / Remove** are on the list (and the asset page). Same permission as Add (`write_assets`: analyst, engineer, admin). Viewers only read.

The Assets filter bar has **one Search** box (number, Asset ID, hostname, IP, VLAN) and, on the same row, five dropdowns: **Type** (every listed type plus saved custom types), **Source** (**Forge** = added here by hand, discovery or scan; **NetBox**; **Zabbix**, which also includes rows linked to a Zabbix host), **Site**, **VLAN** and **Customer** (the distinct *Site / DC / room*, *VLAN* and *Customer / domain* values already saved on assets). Search and every dropdown combine — all must match (`/assets?q=sw&type=Switch&source=netbox&site=DC-East&vlan=10&customer=Acme`). **Reset** clears them all. VLAN is an inventory note (`assets.extras.vlan`) — ForgeSRE never reads or changes switch VLANs.

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

The Assets table shows **Ping** and **:9100 / :9182 / snmp :161** color dots after the IP (the SNMP dot shows the asset's own port, e.g. `snmp :1161`; a host with both an HTTP exporter and SNMP gets a small `snmp :port` tag next to the exporter dot; nothing extra when SNMP is not configured). They are last-known (or yellow until the first probe). The list page is not blocked; a background `GET /api/v1/assets/reachability` refreshes them (`./forgesre ping` / `asset_probe`).

- **green** — last probe succeeded (ICMP reply, or exporter `/metrics` / SNMP GET).
- **yellow** — not probed yet, skipped, or unknown.
- **red** — last probe failed (no ICMP reply, exporter down, or SNMP no reply).
- Web/appliance rows are inventory only until you set a scrape address yourself. They are **not** SNMP-scraped.
- `source=manual`.
- If owner email is set, new incidents notify that address (see §11).

On the asset page, **Detect OS / scrape port** re-runs the same probe and fills type + scrape. You can override afterwards.

The same page has a **right-hand Machine metrics** panel (two columns; stacks on a narrow window). Each metric is **one short row**: name · a small square (green / yellow / red) · the % (or `up` / `down` for Collecting). Hover a row for its alarm % (or the Collecting detail); a disabled alarm shows `off`. Glance at bundled class metrics from Prometheus: Linux `node_` CPU/mem/disk, Windows `windows_` the same idea, network SNMP `up` only. Tiles match the same way verify does (`up{asset="<id>"}` first, then hostname, then `instance=<scrape>` such as `IP:9182`). Missing series stay **yellow** (`not collecting`) — never a fake 0%. When verify PROM is PASS, Collecting is green. Colors follow this asset’s Add/Edit checklist (else bundled alerts/playrules: Linux CPU **95%**, Linux/Windows memory and disk **90%**, Windows CPU **90%**, demo gauges **80%**). **One Edit Alarms** on the panel head opens that host’s Alarms (not an Edit on every tile). `forge-demo-*` rows and discovery seed `10.20.30.41` are labeled **DEMO**. Grafana is unchanged (graphs only; not the alarm path).

On **Add asset / Edit** (not a column on the Assets **list**), the form is three columns. **Left:** *Identity* (Asset ID | Hostname | IP first — same order as the Assets table, skipping `#` — then type, environment, Customer / domain, Site / DC / room, VLAN, notes), *NOC contacts* (owner / team, contact, owner email + phone, Backup on-call name / phone / email, Support hours, Timezone, License / contract / SLA, Runbook note) and *Support coverage* (Under support: Yes / Internal / No / **Custom** — Custom needs a short note such as `weekdays 8–16`; the pill reads **Custom** + the note unless the dates say Expiring / Out of support; reminder only, no incident). **Middle:** *Comms / monitoring* only (scrape address `host:port`, SNMP port, SNMP version, Community / v3 USM); the empty space under it is reserved for later fields. **Right:** *Standard alarms* (cpu / mem / disk / up, enable + threshold %), a rule, then *Client playrules*. The **Runbook note** is guidance for the human on shift at 3am — never executed, not a Playrule, not a threshold. Keep type **Auto (detect exporter)**. The NOC fields live in `assets.extras` (JSON, never written to NetBox), show on the incident **Who to call** card (Runbook note as a highlighted block) and in the escalation mail; role steps still mail Owner email only. **Client playrules** (`assets.playrule_ids`) do not create Prometheus thresholds: when that alertname opens a new incident on this asset, the first ticked enabled playrule with the same alertname wins (timeline: `(asset client playrule)`); nothing ticked or no match = the global first-by-id alertname match, unchanged. Clone copies both (new Asset ID only). Detect also shows which bundled families the exporter actually exposes. Stored on the asset (`alarms` JSON). **Save** and **Cancel** are at the bottom (Cancel returns to the asset page or the list). Tiles use that threshold for red/green. Prometheus alert rules stay global — ForgeSRE skips opening an incident for a bundled alert when the alarm is disabled or the webhook value is below the asset threshold. Until you change `monitoring/alerts.yml` / `alerts.local.yml`, Prometheus may still fire; the incident list stays quiet.

API: `POST /api/v1/assets` with JSON `hostname`, optional `asset_id` (else a slug from hostname for API/discovery), `ip`, `type` (`Auto (detect exporter)` to probe), `environment`, `owner`, `contact_name`, `owner_email`, `owner_phone`, `notes`, optional `scrape_address`. Detect-only: `GET /api/v1/detect-exporter?ip=`. List ping/exporter colors: `GET /api/v1/assets/reachability` (async; the HTML list is last-known). Verify live path: `GET /api/v1/assets/{id}/verify` and `GET /api/v1/verify` (`write_assets`) — empty scrape + Unknown still probes `:9100`/`:9182` and **saves** type + scrape when `/metrics` has `node_` / `windows_`. Glance tiles: `GET /api/v1/assets/{id}/metrics` (`read_assets`). Update: `POST /api/v1/assets/{asset_id}` (hostname, type, IP, scrape, contacts; id stays). Clone: `POST /api/v1/assets/{id}/clone`. Delete: `POST /api/v1/assets/{id}/delete`.

Similar-incident history on the asset page groups past incidents by alert/title (count, open count, last seen). Seed already puts a closed HighCPU on `forge-demo-01` so this is visible after install.

### B. Discovery (scan the management network)

Who: **analyst**, engineer, or admin to scan and Approve. After Approve, fill contacts on the asset page — discovery does not guess who owns the box. You can still add **manual Assets** on `/assets` without ever running discovery — inventory stays the monitoring source of truth.

1. Open **Discovery**. Prefill is this appliance’s **primary IPv4 as `/24`** (host network / Core `network_mode: host`). ForgeSRE does **not** silently scan every connected VLAN.
2. Prefill is a suggestion only. Type or keep the CIDR(s), then **Confirm & scan** — that writes `discovery.cidrs` in live `config/forgesre.yml` and queues a background probe. **Scan now** stays disabled until a CIDR is confirmed (empty `discovery.cidrs` = **no scan**). You can still edit YAML by hand; Core reloads cidrs on Confirm without `install.sh`.

```yaml
discovery:
  enabled: true
  mode: semi-automatic   # manual | semi-automatic | automatic
  cidrs: ["10.20.30.0/24"]   # empty = no scan
```

3. **Confirm & scan** / **Scan now** enqueue kind `discovery_scan` on the existing Postgres `jobs` table (`status=pending`). The HTTP handler **never** runs `run_scan` inline — it always **redirects** with a flash. `_jobs_loop` / `run_pending_jobs` executes the TCP/SNMP/`/metrics` probe in try/except. A probe exception sets `job.error` and a Journal row; Core/uvicorn stay up. **Not nmap**, and **not** NetBox. Ports: TCP **22, 80, 443, 9100, 9182** + SNMP **UDP/161**. Limits: **256 hosts per CIDR**, **1024 total**. **Sync NetBox** is a separate admin CTA (read-only), shown **side-by-side** (~50%) with Scan now. **Confirm & scan** and **Scan now** sit on one row. Long helper text is in the ⓘ tip. Background loop uses confirmed YAML cidrs only. Hosts already in Assets are skipped. Results land in the candidate table (10 per page) and in Journal module `discovery`. ICMP ping is on Assets, not Scan now. There is **no Celery**.
4. Banner **NEW DEVICE DETECTED**. Found hosts stay on **Waiting for Approve** until you decide. They are **not** auto-added to inventory.
5. Candidate table shows **Open ports**, **node_exporter** (yes/no: :9100 + `node_` metrics), **windows_exporter** (yes/no: :9182 + `windows_` metrics), and **SNMP** (`snmp_ok`).
6. **Approve** (on the Discovery page) → creates an asset (`source=discovery`, id like `disc-10-20-30-41`) and sends you to the asset page. **Ignore** rejects the host (out of inventory).


### C. Bundled NetBox (read-sync)

`docker compose up -d` / `./forgesre update` starts NetBox with **no compose profile**. Image pin: `netboxcommunity/netbox:v4.6.9-5.0.2` (the old `v4.4-3.2.0` tag is not on Docker Hub). UI: `http://<VM-IP>:8001`. First login: user `admin`, email `admin@forgesre.local`, password `NETBOX_SUPERUSER_PASSWORD` in `secrets/secrets.env` (not an operator personal email). First boot pulls a large image then runs Django migrations and can take several minutes — **System Health** / `./forgesre doctor` stays **yellow / starting** until `http://127.0.0.1:8001/login/` answers. That is not a fake green.

**Core is not a NetBox UI login.** `NETBOX_API_TOKEN` is a read-only token on the Django **superuser** (`NETBOX_SUPERUSER_NAME`, usually `admin`) with `is_superuser` and `dcim | device | Can view device`. ForgeSRE does not create a separate NetBox user named Core. A valid token on a user **without** that permission is still HTTP **403** on `GET /api/dcim/devices/`. Prefer a NetBox UI **v2** token in secrets (see below). A legacy 40-character **v1** value is still upserted on every NetBox start (`scripts/netbox-upsert-token.py`). `docker compose logs netbox | grep forgesre` shows **`v2 token` / `skipping v1 upsert`** when secrets already hold `nbt_…`, or **`v1 token ready`** for the fallback. **`could not upsert`** means a v1 secret never reached the NetBox DB (UI still starts).

On **System Health** (`/health-ui`) the NetBox tile follows the Discovery traffic light, not “paused”: **starting** (yellow) while first-boot migrations run; **warn** (yellow) when the UI answers but the devices API is 403 / token empty; **running** (green) when the API is 200. A bundled NetBox UI that is up must not show **paused**. **SNMP exporter** is a different tile: with **no** Network device + IP it is **paused (no SNMP targets)** — that is idle, not broken, and you do not need to add devices to un-pause it.

Core finds it via compose env `NETBOX_URL=http://127.0.0.1:8001` and `NETBOX_API_TOKEN` from `secrets/secrets.env` (Core reads the bind-mounted secrets file; do not interpolate an empty project `.env` over `env_file`). Discovery only mentions `--netbox-url` / an external instance when `inventory.netbox.url` is **not** localhost. When `NETBOX_API_TOKEN` is still a **plain** 40-character **v1** value, launch upserts it (`plaintext` column, `write_enabled=False`, on `NETBOX_SUPERUSER_NAME`, permission to list devices) via `scripts/netbox-upsert-token.py` — success log **`v1 token ready`**. When the secret looks like **v2** (`nbt_…`), launch **skips** that upsert (`skipping v1 upsert`) and Core sends `Authorization: Bearer`. **`could not upsert`** (UI still starts) means a v1 secret **never landed in the NetBox DB** — Discovery stays HTTP 403 and that is honest. The helper logs the Python exception type and message (never the token). NetBox 4.5+ has no `User.is_staff`; touching it was why upsert failed on a live v4.6 database. NetBox v4.6 defaults to hashed **v2** tokens (`key` is a 12-character public id + HMAC digest); writing the secret into `key` is why `GET /api/dcim/devices/` stayed HTTP **403**. netbox-docker 5.0.2 also skips `SUPERUSER_API_TOKEN` unless `SUPERUSER_API_KEY` is set, and first-insert-only superuser creation does not help an existing database. After `git pull origin main && ./forgesre update` (recreates `netbox` so launch runs), Core sync is GET-only. On **Discovery**, the NetBox light is a non-clickable status chip (`role=status`, CSS color, not a `<button>`; **Sync NetBox** is the only action) from `GET /api/dcim/devices/` (not a write): **grey** **Not connected** / **API 403** (UI down, API 403, no token); **yellow** **No devices** (API 200 and count == 0; empty is normal). Sentence: *No devices yet; add in NetBox UI `:8001` or use Assets/Discovery.* **green** **Connected** (API 200 and count ≥ 1). Empty must not look like 403. HTTP 200 with a v2 token is yellow or green — do not recreate a v1 token. HTTP 403 with `API token: yes` means NetBox rejected the token Core already has (often a 12-character key instead of the full `nbt_…` secret, or core not recreated after `secrets.env`) — recreate **core** for v2, **netbox+core** for v1; `API token: no` means the token is empty. Admin **Sync NetBox** is clickable on yellow and green; grey still allows a retry click when the UI answers (`/login/` / `/api/status/`) — a devices 403 is a warning, not `disabled`. First-boot (UI not answering) stays disabled with one sentence. Engineers see a disabled CTA and **Admin only.** Devices become local assets (`source=netbox`): asset id = slug of the NetBox name (lowercase, digits, hyphens; `nb-<id>` if empty), type **Auto (detect exporter)**, **empty** scrape address and monitoring profile (no `linux-standard` / `:9100` guess — run **Verify** or edit the row), no owner. NetBox site / tenant / role / tags / model go into **notes** once, at first import (read-only copy, not kept in sync). An asset that already exists (matched by NetBox id, then by slug) keeps its owner, contact, email, notes, type and scrape; sync only fills an empty NetBox id/source and an empty IP. The flash says *N new, M already linked*. Automatic sync runs ~30 s after Core start and every 6 h when `inventory.netbox.auto_sync: true` (default); set it `false` for manual **Sync NetBox** only — Discovery says which mode is on. Core **never writes** back to NetBox and is not a CMDB writer. Local inventory stays the monitoring source of truth. Do **not** re-run `./install.sh` (that regenerates secrets).

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

**After `git pull origin main && ./forgesre update`** the `snmp-exporter` container should stay **up**: `snmp.yml` now carries a valid snmp_exporter v0.26 `if_mib` module (ifDescr / ifName / ifAlias as per-metric lookup labels; ifAdminStatus, ifOperStatus, ifHCInOctets, ifHCOutOctets, sysUpTime as metrics) instead of the old module-level `lookups`, which v0.26 rejects at start-up. `./forgesre render-monitoring` always POSTs `127.0.0.1:9116/-/reload` after writing `snmp.yml`; if the exporter is not answering yet, run `docker compose restart snmp-exporter` (no install.sh). From then on **SnmpDeviceUnreachable** (`up{job="forgesre-snmp"} == 0`) means the device itself did not answer the walk (UDP/161, ACL, community or v3 user), not a broken exporter config. Testing it end to end needs a real SNMP agent (a switch, or `snmpd` on a lab host) as a Network device asset with an IP — the seeded demo switch is not a live SNMP walk.

#### Per-asset SNMP version and auth

Assets → Edit → *Comms / monitoring* has **SNMP version** (v1 / v2c / v3) under SNMP port.

- **v1 / v2c — Community:** *Default* uses `SNMP_COMMUNITY` (snmp.yml auth `public_v2`; v1 Default uses `public_v1`, same community). *Custom* stores a community for this host only.
- **v3 (USM):** username, security level (`noAuthNoPriv` / `authNoPriv` / `authPriv`), auth protocol (MD5 / SHA / SHA224–SHA512) + password, privacy protocol (DES / AES / AES192 / AES256 / AES192C / AES256C) + password. USM passwords are at least 8 characters.
- Stored in `assets.extras` (`snmp_*`). Community strings and v3 passwords are **never** shown again — not on Edit (password boxes, blank = keep the saved one), not on the asset page, not in `GET /api/v1/assets…` (only `snmp_*_set: true`), not in Journal or audit.
- A Custom community or v3 needs its own snmp_exporter auth `forgesre_<asset id>`. `./forgesre snmp-auths` reads them from Core (`GET /api/v1/sd/snmp-auths`, Bearer webhook token), rewrites only the `forgesre-asset-auths` block in `data/generated/snmp.yml` (chmod 600) and POSTs snmp_exporter `/-/reload`. `./forgesre update` and `./forgesre render-monitoring` run it too; if Core does not answer, the block already there is kept.
- **Until that auth is in snmp.yml, SNMP HTTP SD keeps sending `public_v2`** for the host, and the asset page shows *SNMP auth … pending*. Core never points the exporter at an auth it does not have. After Save on a Custom / v3 host, run `./forgesre snmp-auths` (no restart, no install.sh).
- Verify / Ping's quick SNMP GET still uses `SNMP_COMMUNITY` (v2c) — the per-asset auth is for snmp_exporter polling.
- **VLAN** on the same form is an inventory note for search and filters. It does not change which auth or port is polled, and ForgeSRE never writes VLANs to the switch.

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
  → incident INC-0134_16.08.2026_09:13
  → playrule matched by labels.alertname
  → playbook attached
  → notification generated
  → investigate job enqueued (worker runs ForgeRCA; webhook does not wait)
```

Incident is tied to an asset when `labels.asset` or `labels.instance` equals `asset_id` or hostname. Demo alerts use `asset: forge-demo-01`. An alert with neither label is **not** attached to the demo host; it opens with no asset (fingerprint `alertname:unlabeled`).

Statuses: `OPEN` → `INVESTIGATING` (Acknowledge) → `RESOLVED` → `CLOSED`. Unacked time past the first policy step moves `OPEN` / `INVESTIGATING` to `ESCALATED`.

Fingerprint is `alertname:asset`. While an incident with that fingerprint is **active** (`OPEN` / `INVESTIGATING` / `ESCALATED`), another fire updates it — no duplicate.

**Resolved is not Close.** When Alertmanager sends `resolved`, Core sets the active incident to `RESOLVED` (timeline: *resolved by Alertmanager*). It does **not** set `CLOSED`; Close is a human click on the incident page. If the same alert **fires again** after that, Core opens a **new** incident (new number, new escalation clock from step 0) and writes **RE-FIRED** on both timelines (*Same alert fired again after INC-… was RESOLVED* / *Fired again as INC-…*). A `CLOSED` incident is final and never reopens either — the next fire is also a new incident. A flapping alert therefore produces one incident per fire after each resolve; the RE-FIRED links show the chain.

New numbers look like `INC-0134_16.08.2026_09:13` (short seq + local date/time). Older `INC-000012` rows stay valid. Sequence is still `max(seq)+1`, not `count(*)+1`. TAB in `./forgesre` completes those ids after `incidents` / `history`.

Check the RCA queue with `./forgesre jobs`. If a job is `error`, open Console (`/journal`) module `rca`.

---

## 9. Playrules

A **playrule** maps `alertname` → playbook + severity. AI cannot edit playrules. Creating a playrule does **not** create a Prometheus alert.

Who: **analyst** (permission `write_play`). Engineers can read, not create.

`monitoring/alerts.yml` is the **PromQL source** (bundled rules). Asset **Alarms** on Add/Edit (`assets.alarms` enable/%) are a per-host overlay: same Alertmanager webhook, not a second engine. Prometheus may still fire a global rule; ForgeSRE skips opening an incident when the alarm is disabled or the value is below the host threshold.

### Create in the UI

1. Optionally create the playbook first (`/playbooks`).
2. **Playrules** → **Create playrule**.
3. Name (unique), **alertname** (must match Prometheus; the form suggests names from `alerts.yml` and previews that rule's PromQL read-only), severity, playbook, escalation policy. **Operator note — not executed:** metric / operator / value are free text for humans. ForgeSRE never evaluates them; the threshold lives in the Prometheus rule.
4. **Save**. **Cancel** next to Save returns to this page without creating. **Edit** loads the same form (Save updates the row). **Toggle** disables without deleting. **Remove** deletes the playrule; existing incidents keep their playbook, new alerts with that name open without one.

The list column **Fires when (alerts.yml)** shows the PromQL `expr` / `for` read from `monitoring/alerts.yml` + `alerts.local.yml` (`FORGESRE_MONITORING_DIR`). **No Prometheus rule** means no bundled/local rule has that alertname — the playrule will never match until one does.

The form stores `condition.alertname`. Matching on ingest is **alertname only**: the first enabled playrule whose `condition.alertname` equals Prometheus `labels.alertname` (case-insensitive). Metric / operator / value are not used for matching.

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

API: `POST /api/v1/playrules` with `name`, `condition` (object), `playbook_id`, `severity`. Only `condition.alertname` is used for matching.

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

A background loop every 30 seconds generates (and optionally sends) those steps, counted from the incident start, until someone **Acknowledges** (status `OPEN` / `INVESTIGATING` / `ESCALATED` with no ack). Each step is written once per incident. The outbox is `/ops#mail`.

Recipient per step, in order:

1. The step target is an **email address** (policy line `15 oncall@example.com`) → that address.
2. Else the asset’s **owner email** (demo: `platform@forgesre.local`). The body includes contact name and phone; the role (`team` / `team-lead` / `engineer`) stays in the body as the step name.
3. Else **no recipient**: ForgeSRE records the step with status `no-recipient` (no SMTP attempt, Journal `error`, timeline *no recipient (role)*). It does **not** invent `<role>@forgesre.local` addresses. Assets shows a **No owner email** pill and the Dashboard tile/filter `flag=no-email` lists those hosts.

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
| Resolve | analyst+ (`write_incidents`) | Status `RESOLVED` (the problem is gone; Alertmanager `resolved` does the same). A later fire of the same alert opens a new incident |
| Close | analyst+ (`write_incidents`) | Status `CLOSED` — final; the human says the work is done. Records who closed |
| Open ForgeRCA | analyst+ (`read_ai`) or engineer (`investigate`) | Primary CTA to `/ai/INC-…`. Runs builtin ForgeRCA if needed; does not change the host |
| **Send incident report** | analyst / engineer / admin | Emails the current INC snapshot when SMTP is on (`sent`) as HTML + plain text. If SMTP is off, stores `generated` in the **mail outbox** on `/ops#mail`. Replies arrive in the real mailbox, not in ForgeSRE |

The same page lists **who did what** (audit: ack, resolve, notes) and **operator notes**. Mail bodies are on `/ops#mail`, not a second table here. Notes are not a ticket thread and not RCA. Who did what is a list like the others: it scrolls inside its box, page tabs on the left, **Rows 10 / 20 / 50 / 100** on the right (`?audit_page=` / `?audit_per_page=`, default 10), and changing page keeps your scroll position.

**Engineer evidence** (engineer / admin) is one line per stored row. A single click selects a row; **double-click anywhere on the row** — the opened text included — opens or folds it, and **Enter** does the same for the selected row. Press and drag to select text to copy; a drag never folds the row. Nothing is re-queried and the stored evidence is not changed.

The **Workflow** card on the incident is the attached playbook as a plain list — guidance only; nothing is executed or ticked off.

**Incidents** (`/incidents`) defaults to **All** statuses, newest first (filter **Active (not resolved)** for live work, or an exact status / Critical / last days). **History** (`/history`) is the 90-day archive. Escalation is still the policy; its outbox is `/ops#mail`. Console (`/journal`) is still process reports.

Asset health on the dashboard (`healthy` / `warning` / `critical`) follows open incidents on that asset.

**Grafana is not the alarm path.** Incidents come from Prometheus → Alertmanager → Core. Open Grafana only from **System Health**. Grafana is not in the left nav. If Grafana is down, doctor stays **yellow** (warn) — that is not a Prometheus outage and must not write a Journal error. A real Prom or Alertmanager failure journals `core` / `doctor` **error** naming the hop (`Prometheus :9090`, `Alertmanager :9093`), never a vague “Prometheus Stack”.

---

## 13. AI investigation (ForgeRCA)

Open **ForgeRCA** from the incident (primary CTA → `/ai/INC-…`).

The button runs **builtin ForgeRCA immediately** and opens Summary → Root cause → Recommended actions → Facts → Anomalies → Candidate causes → Limitations. Two pills sit at the top: **ForgeRCA** (green when builtin has a result) and **ForgeAI** (green if the LLM rewrote the prose, yellow while the rewrite runs, red if the LLM is off or unreachable). If `ai.enabled` is on, refresh later for ForgeAI. Do not mash Run now.

Jobs: **one worker thread** in Core (Postgres `jobs` table). There is **no Celery**. Kinds: `investigate` (RCA / LLM rewrite) and `discovery_scan` (Discovery Confirm & scan / Scan now). An LLM rewrite can occupy that thread up to `ai.llm.timeout_seconds` (example.yml default 90; live yml is gitignored). Scheduled `/ops` reports run first in the same loop.

You get:

- Facts vs hypotheses vs anomalies
- Evidence IDs (`EV-…`) with PromQL/LogQL for engineers
- A **ForgeSRE confidence score** (not the LLM’s own number)
- Disclaimer: `AI has not modified the system.`

RCA works with `ai.enabled: false` (builtin analyst). To use a local LLM, download the GGUF (not in git) with `./forgesre fetch-llm`. Implementation (hardware, Compose profile `ai`, offline GGUF, jobs, debug): [`llm.md`](llm.md).

Alertmanager ingest **enqueues** an investigate job. The webhook does not wait on the LLM. `./forgesre jobs` lists pending / running / done / error. Demo (`./forgesre demo`) still runs RCA inline so the first-hour path is immediate.

Queries are **per asset**. `forge-demo-01` uses Core demo gauges. A real Linux host uses `node_cpu_seconds_total` / `node_filesystem_*` with `asset="<id>"`. A real Windows host uses `windows_cpu_time_total` / `windows_logical_disk_*`. A network device uses `up{job="forgesre-snmp",asset="<id>"}`. Demo CPU/disk numbers are never overlaid on another host.

Loki: Alloy still only ships **appliance Core logs** labeled `asset=forge-demo-01` / `job=forgesre` as a demo. RCA may use those for the DEMO host, labeled DEMO. **Real inventory hosts have no Loki until later** — limitation is “no host logs shipped”. Empty Loki is not evidence from that VM. There is no second log stack in V0.8. The RCA page shows that one-liner when the limitation is present.

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

Optional discovery path: put `10.10.10.0/24` on Discovery (or put it in `discovery.cidrs`), Scan now, Approve the `10.10.10.50` candidate instead of the manual form.

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

**D. Verify:** force usage or temporarily lower the threshold in `alerts.local.yml`, then **Incidents** should show a new `INC-…` linked to `app-01`, with the playbook name, **Who to call**, and an escalation row in the mail outbox (`/ops#mail`) addressed to the asset owner email if you filled it (status `no-recipient` if you did not). RCA appears a few seconds later (`./forgesre jobs`).

If the incident has no asset, the alert `asset` / `instance` label did not match `asset_id` or hostname.

---

## 16. Operator CLI and API

**Commands:** [`cli.md`](cli.md). `./forgesre help` / `./forgesre help <command>` on the VM. This section is **when to use which**.

| When | Command |
|---|---|
| Live box after `git pull` | `./forgesre update` — never `./install.sh` |
| Bounce containers only (no pull / install / secrets change) | `./forgesre restart` |
| Re-render Prometheus / Alertmanager / snmp.yml only | `./forgesre render-monitoring` |
| Saved a Custom community / SNMPv3 asset | `./forgesre snmp-auths` |
| Stack lights (same as `/health-ui`) | `./forgesre doctor` |
| Appliance report → `data/reports/` | `./forgesre test` |
| Live inventory path (exporter → Prom → AM → Core) | `./forgesre verify` / `./forgesre ping` |
| SNMP targets JSON | `./forgesre snmp` |
| RCA / LLM queue | `./forgesre jobs` (one worker thread, no Celery) |
| Safety copy | `./forgesre backup` |

**Update vs restart.** `update` is the after-`git pull` path: backup, render-monitoring, image pull (unless `--offline`), `docker compose up -d`, `snmp-auths`, doctor. `restart` only runs `docker compose restart` on containers that already exist (Postgres first, Core last, profile-off services skipped). It does not pull, render, back up, or write `.env` / `secrets/`. New code or templates need `update`; a wedged service needs `restart`. Neither runs `./install.sh`. Full side-by-side: [`cli.md` § Update vs restart](cli.md#update-vs-restart).

Every command (`./forgesre help`, TAB completion, and the Administration CLI list show the same set): [`cli.md` § All commands](cli.md#all-commands).

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
- Discovery is TCP 22/80/443/9100/9182 plus SNMP GET on UDP/161, confirmed YAML `discovery.cidrs` only (suggested primary IPv4 `/24`; empty = no scan), **256 hosts/CIDR** and **1024 total**. It does not use TCP/161. SNMP *polling* is still snmp_exporter after Approve.
- Viewer cannot open Playrules, Playbooks, Escalation, Console, or Discovery (403).
- Optional TLS is an example Caddyfile, not a default container.
- NetBox is read-only. Bundled UI is on by default; Core never writes back.
- Zabbix is read-only and optional (§18). Forge never acknowledges, closes, or edits anything in Zabbix.
- Re-running `./install.sh` regenerates secrets. Core will not start on shipped default `SECRET_KEY` / webhook token (`FORGESRE_DEV=1` is tests/lab only).

When that is enough: install ([`install-config.md`](install-config.md)), add people (§5), add servers (§6–7), then add real alerts only when you are ready for incidents (§15). First-hour lab path: Dashboard **Run demo** → HighCPU on `forge-demo-01` (DEMO pill) → Who to call / Escalation. CLI: `./forgesre demo`.

---

## 18. Zabbix (read-only source)

Optional. If you already run Zabbix, ForgeSRE can import its hosts, turn its problems into incidents, and show its trends on hosts that Prometheus does not scrape. The Prometheus path is unchanged and does not need Zabbix.

**Rules that never change:**

- ForgeSRE is **read-only** toward Zabbix. It only calls `apiinfo.version`, `host.get`, `problem.get`, `item.get`, `trend.get`. It never calls `*.create`, `*.update`, `*.delete`, or `event.acknowledge` — the client refuses any other method before it reaches the network.
- No extra container, no Zabbix iframe, no database scrape, no second Prometheus. The integration is Python inside Core plus the existing jobs / discovery loops (same pattern as NetBox).
- If Zabbix is down: each API call times out after 2–5 s (`inventory.zabbix.timeout_seconds`, default 4), the **Zabbix** Health cube turns **yellow** (never red), Console gets **one** `zabbix` / `health` line, and Core backs off for 2 minutes instead of retrying. Dashboard, incidents, Prometheus alerts, and mail keep working.

### A. One-time setup in Zabbix: read-only user and API token

Menu names are Zabbix 6.4 / 7.0. Older versions have the same objects under **Administration**.

1. **Users → User groups → Create user group**: name `ForgeSRE read-only`. On **Host permissions** (6.x: **Permissions**) add the host groups ForgeSRE should see with **Read**. Nothing else. **Add**.
2. **Users → Users → Create user**: username `forgesre-ro`, a long random password, group `ForgeSRE read-only`. On **Permissions** pick role **User role** (type *User*). **Add**.
3. *(Optional, stricter)* **Users → User roles → User role** (or a copy of it): **API methods → Allow list** with `host.get`, `problem.get`, `item.get`, `trend.get`. `apiinfo.version` needs no permission.
4. **Users → API tokens → Create API token**: name `forgesre`, user `forgesre-ro`, expiry as your policy says. **Add**, then copy the token — Zabbix shows it once.

### B. Connect ForgeSRE and import hosts

On the VM, edit `secrets/secrets.env` (never committed; template in `secrets/secrets.example.env` under `# --- Zabbix ---`):

```bash
ZABBIX_URL=https://zabbix.example.local/zabbix   # frontend base; /api_jsonrpc.php is added
ZABBIX_API_TOKEN=<token from step A.4>
ZABBIX_WEBHOOK_TOKEN=<openssl rand -hex 24>      # needed for section C; empty = webhook off
```

Then:

```bash
./forgesre update
```

Never `./install.sh` (it regenerates secrets). Health shows the **Zabbix** cube: green **Connected**, yellow **Unreachable / API error / No hosts visible**, grey **Not configured**.

**Discovery → Zabbix → Sync hosts** (admin) runs `host.get` once:

- New hosts become assets with type **Auto**, **empty scrape** (ForgeSRE never invents `:9100`), source `zabbix`, and the Zabbix host ID stored. They do not appear in Prometheus HTTP SD until you set a scrape target yourself (§7).
- Existing assets are matched by Zabbix host ID, then IP, then hostname. A host already imported from NetBox or Discovery is **linked**, not cloned. Linking only fills empty fields; owner, email, phone, contact, notes, and anything an operator typed are never overwritten.
- Host groups fill extras **customer** (first non-generic group; `Customer/Site` nesting also fills **site**) only when those extras are empty.
- The agent state (up / down / unknown) shows as a pill on **Assets** with filter chips **Zabbix / agent up / agent down / agent unknown** (`/assets?source=zabbix&agent=down`). Agent state is refreshed at most once every 5 minutes, with one light `host.get` for linked hosts only.

The Discovery block shows the URL, status, the last sync result, and the last error. Each sync writes one `zabbix` / `sync` line in Console.

**Auto-sync:** off by default. To import with the existing discovery loop (about every 6 h), set in `config/forgesre.yml`:

```yaml
inventory:
  zabbix:
    auto_sync: true
    timeout_seconds: 4   # clamped 2–5
    # enabled: false     # force off even if secrets are set
```

then `./forgesre update`.

### C. Problems become incidents (webhook media type + Action)

ForgeSRE listens on `POST http://<FORGE-IP>:8080/api/v1/webhooks/zabbix` (alias `/webhooks/zabbix`) with its **own** Bearer token `ZABBIX_WEBHOOK_TOKEN` (not the Alertmanager token). Wrong token → 401; token not set → 503; missing trigger name or host → 422.

**1. Media type.** **Alerts → Media types → Create media type**: name `ForgeSRE`, type **Webhook**. Parameters (name → value):

| Name | Value |
|---|---|
| `forge_url` | `http://<FORGE-IP>:8080/api/v1/webhooks/zabbix` |
| `forge_token` | the `ZABBIX_WEBHOOK_TOKEN` value |
| `event_value` | `{EVENT.VALUE}` |
| `event_status` | `{EVENT.STATUS}` |
| `event_id` | `{EVENT.ID}` |
| `event_name` | `{EVENT.NAME}` |
| `event_severity` | `{EVENT.SEVERITY}` |
| `event_opdata` | `{EVENT.OPDATA}` |
| `event_date` | `{EVENT.DATE}` |
| `event_time` | `{EVENT.TIME}` |
| `trigger_id` | `{TRIGGER.ID}` |
| `trigger_name` | `{TRIGGER.NAME}` |
| `host_host` | `{HOST.HOST}` |
| `host_name` | `{HOST.NAME}` |
| `host_ip` | `{HOST.IP}` |
| `host_id` | `{HOST.ID}` |

Script:

```javascript
var p = JSON.parse(value);
var body = {};
Object.keys(p).forEach(function (key) {
    if (key !== 'forge_url' && key !== 'forge_token') {
        body[key] = p[key];
    }
});
var req = new HttpRequest();
req.addHeader('Content-Type: application/json');
req.addHeader('Authorization: Bearer ' + p.forge_token);
var resp = req.post(p.forge_url, JSON.stringify(body));
if (req.getStatus() < 200 || req.getStatus() >= 300) {
    throw 'ForgeSRE HTTP ' + req.getStatus() + ': ' + resp;
}
return 'OK';
```

Timeout `10s`. On **Message templates** add *Problem* and *Problem recovery* (the text is not used, Zabbix just needs a template). **Add**, then **Test** — a 200 with `"accepted": true` means the token and URL are right.

The JSON ForgeSRE receives (keys are case-insensitive; `EVENT.VALUE`, `event_value`, `eventValue` are the same):

```json
{
  "event_value": "1",
  "event_status": "PROBLEM",
  "event_id": "81234",
  "event_name": "High CPU utilization (over 90% for 5m)",
  "event_severity": "High",
  "event_opdata": "Current utilization: 97 %",
  "trigger_id": "23456",
  "trigger_name": "High CPU utilization (over 90% for 5m)",
  "host_host": "app-01",
  "host_name": "app-01 (prod)",
  "host_ip": "10.20.1.15",
  "host_id": "10584"
}
```

Mapping:

- `event_value` `1` = problem (opens or keeps the incident), `0` = recovery (incident → **RESOLVED**, timeline "resolved by Zabbix").
- `alertname` = `trigger_name`, or `event_name` if the trigger name is empty.
- Severity: Disaster / High → **CRITICAL**; Average / Warning → **WARNING**; Information / Not classified → INFO.
- Fingerprint `zabbix:{TRIGGER.ID}:{HOST.HOST}` — the same problem never opens two incidents; the recovery closes the one it opened.
- Asset: Zabbix host ID first, then `HOST.HOST` as asset ID / hostname, then `HOST.IP`. Prometheus alerts are matched exactly as before.

Incidents keep the normal lifecycle (playrule, playbook, Who to call, escalation mail, ForgeRCA) and carry a small **Zabbix** pill (Prometheus incidents show **Prometheus**) on the Incidents list, the incident header, and the mail header.

**2. Media on the user.** **Users → Users → forgesre-ro → Media → Add**: type `ForgeSRE`, send to `forgesre`, all severities, enabled. Zabbix only sends for hosts the user can read, so the host groups from step A.1 also scope the webhook.

**3. Action.** **Alerts → Actions → Trigger actions → Create action**: name `ForgeSRE`. Conditions as you like (for example *Host group equals Production*; none = everything forgesre-ro can read).

- **Operations → Add**: send to user `forgesre-ro`, only via `ForgeSRE`.
- **Recovery operations → Add**: send to user `forgesre-ro` via `ForgeSRE` (or *Notify all involved*).
- **Update operations**: leave empty.

**Backup poll.** When `ZABBIX_WEBHOOK_TOKEN` is set, Core also asks `problem.get` every 3 minutes for the triggers of **open Zabbix incidents only** (older than 2 minutes). If Zabbix no longer has an open problem for that trigger, the incident is resolved through the same fingerprint and Console gets a `zabbix` / `poll` line. This covers a lost recovery webhook; it does not open incidents (only the webhook does). No webhook token = no poll.

### D. Playrules for Zabbix problems

A playrule matches the **exact trigger name** (`alertname`), same as for Prometheus. **Playrules → New**: name = the trigger name as it appears in Zabbix **Problems**, for example `High CPU utilization (over 90% for 5m)`. Expanded macros count: a trigger called `Load on {HOST.NAME}` reaches ForgeSRE as `Load on app-01`. Playrule logic is not Zabbix-specific — nothing else changes.

### E. Graphs from Zabbix trends

On the incident page (**Host metrics** card) and Dashboard **Host metrics**, an asset with a Zabbix host ID and **no Prometheus samples** shows CPU / memory / disk for the last 24 h from `trend.get` (hourly averages, items `system.cpu.util`, `vm.memory.utilization`, `vfs.fs…pused`). The request is made only when you open the graph, with the 2–5 s timeout, and cached for 5 minutes per host. If Zabbix is slow, down, or the host has no such items, the card says **No samples yet**. Scraped assets keep their Prometheus graphs. ForgeSRE never pulls trends for all hosts in the background.

### F. What ForgeSRE never writes back

- No acknowledge, no close, no comments, no severity changes on Zabbix problems. Acknowledging or resolving in ForgeSRE stays in ForgeSRE.
- No host, group, template, item, trigger, user, media type, or action is created, changed, or deleted. Removing an imported asset in ForgeSRE does not touch Zabbix (a later sync imports it again unless you remove it from the user's host groups).
- ForgeSRE stores only the Zabbix URL, the API token (in `secrets/secrets.env`), and per asset the Zabbix host ID and agent state.
