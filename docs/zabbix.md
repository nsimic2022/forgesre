# Connecting Zabbix to ForgeSRE

Step-by-step cookbook: every click in Zabbix and every step on the ForgeSRE VM so that Zabbix **hosts** become ForgeSRE **assets**, Zabbix **problems** become ForgeSRE **incidents**, and Zabbix **trends** show up as graphs.

Zabbix is optional. The Prometheus path (node_exporter, windows_exporter, snmp_exporter → Alertmanager → incidents) works without it and does not change when you add it.

Short version for people who have done this before:

1. Zabbix: user group with **Read** on the host groups → user `forgesre-ro` with **User role** → API token.
2. Forge VM: `ZABBIX_URL`, `ZABBIX_API_TOKEN`, `ZABBIX_WEBHOOK_TOKEN` in `secrets/secrets.env` → `./forgesre update`.
3. Forge UI: **Discovery → Zabbix → Sync hosts** → fill **Owner email** on the new assets.
4. Zabbix: media type **ForgeSRE** (Webhook, script below) → media on `forgesre-ro` → trigger action with problem + recovery operations.
5. Test the webhook (HTTP 200), then add playrules whose **alertname** is the exact Zabbix trigger name.

Never done this? Follow the numbered [end-to-end checklist](#18-end-to-end-operator-checklist) after the click-paths below.

---

## Contents

1. [What ForgeSRE does and does not do with Zabbix](#1-what-forgesre-does-and-does-not-do-with-zabbix)
2. [Prerequisites](#2-prerequisites)
3. [Zabbix: read-only user group, user and API token](#3-zabbix-read-only-user-group-user-and-api-token)
4. [ForgeSRE: secrets and update](#4-forgesre-secrets-and-update)
5. [ForgeSRE: Discovery → Sync hosts](#5-forgesre-discovery--sync-hosts)
6. [ForgeSRE: owner email and NOC fields on imported assets](#6-forgesre-owner-email-and-noc-fields-on-imported-assets)
7. [Zabbix: webhook media type](#7-zabbix-webhook-media-type)
8. [Zabbix: media on the user and the trigger action](#8-zabbix-media-on-the-user-and-the-trigger-action)
9. [Test the webhook](#9-test-the-webhook)
10. [ForgeSRE: playrules for Zabbix triggers](#10-forgesre-playrules-for-zabbix-triggers)
11. [Where Zabbix data shows up in ForgeSRE](#11-where-zabbix-data-shows-up-in-forgesre)
12. [Optional: automatic host import](#12-optional-automatic-host-import)
13. [Troubleshooting](#13-troubleshooting)
14. [Reference: payload and mapping](#14-reference-payload-and-mapping)
15. [Standard alarms vs Zabbix incidents](#15-standard-alarms-vs-zabbix-incidents)
16. [Acknowledge, resolve, close — no write-back](#16-acknowledge-resolve-close--no-write-back)
17. [CLI commands that matter](#17-cli-commands-that-matter)
18. [End-to-end operator checklist](#18-end-to-end-operator-checklist)
19. [After git pull](#19-after-git-pull)

---

## 1. What ForgeSRE does and does not do with Zabbix

**Does:**

| Data | How | When |
|---|---|---|
| Hosts → assets | API `host.get` (with interfaces and host groups) | **Discovery → Sync hosts** (admin), or every ~6 h with `auto_sync: true` |
| Agent availability (up / down / unknown) | API `host.get`, interfaces only, linked hosts only | At most every 5 minutes |
| Problems → incidents | Zabbix **webhook** media type calls `POST /api/v1/webhooks/zabbix` | Each problem and each recovery |
| Missed recoveries | API `problem.get` for triggers of **open Zabbix incidents only** | Every 3 minutes, only when `ZABBIX_WEBHOOK_TOKEN` is set |
| CPU / memory / disk graphs | API `item.get` + `trend.get` (hourly averages, last 24 h) | Only when you open a graph for an asset that has **no Prometheus samples**; cached 5 minutes per host |
| Status light | API `apiinfo.version` + `host.get` count | Health cube **Zabbix** and the Discovery Zabbix card (cached 1–2 minutes) |

**Does not:**

- **No write-back.** ForgeSRE never writes back to Zabbix. It only calls `apiinfo.version`, `host.get`, `problem.get`, `item.get`, `trend.get`. The client refuses any other method (`*.create`, `*.update`, `*.delete`, `event.acknowledge`) before a request is built.
- **No acknowledge, close, comment, or severity change in Zabbix.** Acknowledging or resolving an incident in ForgeSRE stays in ForgeSRE. The Zabbix problem stays as it is until Zabbix itself recovers it.
- No host, group, template, item, trigger, user, media type, or action is created or changed in Zabbix. You create those by hand (sections 3, 7, 8).
- No Zabbix iframe or embedded Zabbix UI. The Health page only links to `ZABBIX_URL`.
- No Zabbix database access, no extra container, no Zabbix proxy or agent on the ForgeSRE VM.
- No `./forgesre zabbix` CLI command. Everything is the secrets file, `./forgesre update`, and the web UI.
- No SNMP settings are needed for Zabbix. `./forgesre snmp-auths` and the asset SNMP fields are for ForgeSRE's own snmp_exporter, not for Zabbix.
- Problems are not pulled. Incidents are opened **only** by the webhook. The 3-minute `problem.get` poll can only resolve an incident whose recovery webhook was lost.
- **Grafana is not in the alarm path.** Zabbix problems never go through Grafana. Alarm path for Prometheus stays exporter → Prometheus → Alertmanager webhook → Core. Zabbix is a **second ingest path** (JSON-RPC read + webhook). Open Grafana from System Health for extra graphs only.
- Asset **Standard alarms** (CPU / memory / disk / up checkboxes) only mute bundled **Prometheus** alerts. They do not mute Zabbix triggers ([section 15](#15-standard-alarms-vs-zabbix-incidents)).
- There is **no Administration form** to paste the Zabbix URL or tokens. `/admin` is users, backup, and CLI cheat sheet. Tokens go in `secrets/secrets.env` ([section 4](#4-forgesre-secrets-and-update)).

If Zabbix is down: each API call gives up after 2–5 s, the **Zabbix** Health cube turns **yellow** (never red), Console gets **one** `zabbix` / `health` line, and ForgeSRE stops calling Zabbix for 2 minutes instead of retrying. Dashboard, incidents, Prometheus alerts, and mail keep working.

---

## 2. Prerequisites

Two machines: the **Zabbix server you already run**, and the **ForgeSRE appliance** (one Ubuntu VM, Docker Compose, **host networking**).

### 2.1 Appliance is up

On the Forge VM, before touching Zabbix:

```bash
cd ~/forgesre   # or wherever the clone lives
./forgesre doctor
./forgesre status
```

| Check | What “ready” looks like |
|---|---|
| `./forgesre doctor` / **System Health** (`/health-ui`) | **Core** green. Postgres, Prometheus, Alertmanager green (or at least not red). Grafana yellow is graphs-only — **not** the alarm path. The **Zabbix** cube is **grey Not configured** until section 4. |
| `curl -fsS http://127.0.0.1:8080/api/v1/health` | HTTP 200 (use the port in `.env` `FORGESRE_HTTP_PORT` if you changed it). |
| Login | `http://<FORGE-IP>:8080` with the admin from `installation-report.md` / `secrets/secrets.env`. |

Day-2 on a live VM is `git pull origin main && ./forgesre update`. **Never** `./install.sh` again — it regenerates secrets.

### 2.2 Ports and host networking

Core runs with `network_mode: host` and listens on **`0.0.0.0:${FORGESRE_HTTP_PORT:-8080}`** (see `.env` and `docker-compose.yml`). There is no Docker-published port and no container IP to aim at. The Zabbix **server process** (the one that runs media-type webhooks — `zabbix_server`, not the nginx/Apache frontend) must reach **the Forge VM's management IP** on that TCP port.

| Direction | Port | Why |
|---|---|---|
| Browser / operator → Forge | TCP **8080** (`FORGESRE_HTTP_PORT` in `.env`) | UI + API, including `POST /api/v1/webhooks/zabbix` |
| Zabbix **server** → Forge | same TCP **8080** | Webhook. `localhost` in the media type is the Zabbix box, not Forge. |
| Forge → Zabbix **frontend** | TCP **443** (or 80) | JSON-RPC `host.get` / `item.get` / `trend.get` / `problem.get` at `…/api_jsonrpc.php` |
| Grafana `:3000` | not used | Not in the Zabbix or Prometheus alarm path |

If you put HTTPS in front of Core (`./forgesre tls`), the media-type URL is that HTTPS URL, not `:8080`. If `FORGESRE_HTTP_PORT` is not `8080`, every `8080` in this guide is that value.

Firewall on the Forge VM (example): `sudo ufw allow 8080/tcp` from the Zabbix server's address. Prometheus (`:9090`) and Alertmanager (`:9093`) stay on `127.0.0.1` — Zabbix does not call them.

### 2.3 Two files on the Forge VM (do not mix them)

| File | What belongs there | Zabbix keys |
|---|---|---|
| **`secrets/secrets.env`** | Tokens, passwords, `SECRET_KEY`. Template: `secrets/secrets.example.env` under `# --- Zabbix ---`. Core reads this via `FORGESRE_SECRETS_FILE`. | `ZABBIX_URL`, `ZABBIX_API_TOKEN`, `ZABBIX_WEBHOOK_TOKEN` |
| **`.env`** (repo root) | Ports, paths, compose. Template: `.env.example`. | **None.** `FORGESRE_HTTP_PORT=8080` is the Core listen port the webhook hits. Do not put `ZABBIX_*` here. |
| **`config/forgesre.yml`** | Non-secret YAML. Template: `config/forgesre.example.yml` → `inventory.zabbix`. | Optional `url` (overrides `ZABBIX_URL`), `auto_sync`, `timeout_seconds`, `enabled: false`. **Never** the API token or webhook token. |

Do not merge the two env files. Do not commit the live copies.

### 2.4 Other requirements

| Item | Requirement |
|---|---|
| Zabbix version | **5.4 or newer** (API tokens and the webhook `HttpRequest` object). Menus below are Zabbix **6.4 / 7.0**; see the menu table for 6.0. ForgeSRE picks the auth style itself: `Authorization: Bearer` on 6.4+, the JSON-RPC `auth` field on older servers. |
| Zabbix frontend URL | Use **HTTPS** (the API token travels in every request). Plain HTTP works, but only on a trusted management network. |
| Forge → Zabbix | The ForgeSRE VM reaches the Zabbix **frontend** (TCP 443, or 80) — the URL that serves `api_jsonrpc.php`. |
| Zabbix → Forge | The **Zabbix server** (the process that runs webhooks, not the frontend) reaches ForgeSRE on **TCP 8080** (`http://<FORGE-IP>:8080`). If you put HTTPS in front of Core (`./forgesre tls`), use that URL instead. |
| Zabbix rights | A Zabbix **Super admin** to create the user group, user, API token, media type, and action. |
| ForgeSRE rights | **admin** for Sync hosts; **analyst** (or admin) for asset edits and playrules. |
| Shell on the Forge VM | To edit `secrets/secrets.env` and run `./forgesre update`. |

Quick network checks:

```bash
# On the ForgeSRE VM — must print a version string, e.g. {"jsonrpc":"2.0","result":"7.0.5","id":1}
curl -sS -X POST https://zabbix.example.local/zabbix/api_jsonrpc.php \
  -H 'Content-Type: application/json-rpc' \
  -d '{"jsonrpc":"2.0","method":"apiinfo.version","params":{},"id":1}'

# On the Zabbix server — any HTTP answer (even 401 / 503) proves the port is open
curl -sS -o /dev/null -w '%{http_code}\n' -X POST http://<FORGE-IP>:8080/api/v1/webhooks/zabbix
```

Zabbix menu names by version:

| Object | Zabbix 6.4 / 7.0 | Zabbix 6.0 / 5.4 |
|---|---|---|
| User groups | **Users → User groups** | **Administration → User groups** |
| Users | **Users → Users** | **Administration → Users** |
| User roles | **Users → User roles** | **Administration → User roles** |
| API tokens | **Users → API tokens** | **Administration → General → API tokens** |
| Media types | **Alerts → Media types** | **Administration → Media types** |
| Trigger actions | **Alerts → Actions → Trigger actions** | **Configuration → Actions → Trigger actions** |

---

## 3. Zabbix: read-only user group, user and API token

### 3.1 User group with Read only

1. **Users → User groups → Create user group** (top right).
2. **User group** tab:
   - **Group name:** `ForgeSRE read-only`
   - **Frontend access:** *System default*.
   - **Enabled:** checked.
3. **Host permissions** tab (6.0: **Permissions**):
   - Click **Add** (or **Select**), pick every host group ForgeSRE should import — for example `Customers/ACME`, `Production`, `Network`.
   - Permission: **Read**. Not *Read-write*.
   - Do **not** add `Zabbix servers` unless you want the Zabbix server itself as an asset (its interface is usually `127.0.0.1`; see [troubleshooting](#duplicate-or-odd-hosts)).
4. Leave **Template permissions**, **Problem tag filter**, and the other tabs empty.
5. Click **Add**.

The host groups chosen here are the only thing that decides **which hosts are imported** and **which problems reach ForgeSRE** (Zabbix only sends a webhook for hosts the receiving user can read).

### 3.2 User `forgesre-ro` with User role (not Admin)

1. **Users → Users → Create user**.
2. **User** tab:
   - **Username:** `forgesre-ro`
   - **Name / Last name:** optional (`ForgeSRE`, `read-only`).
   - **Groups:** click **Select**, tick `ForgeSRE read-only`.
   - **Password / Password (once again):** a long random password. Nobody needs to log in with it; ForgeSRE uses the API token.
3. **Permissions** tab:
   - **Role:** click **Select**, pick **User role** (type *User*).
   - Not *Admin role*, not *Super admin role*. ForgeSRE never needs more than read.
4. **Media** tab: leave empty for now (section 8 adds it after the media type exists).
5. Click **Add**.

*(Optional, stricter.)* **Users → User roles → User role → Clone** (call it `ForgeSRE API read`), then on the clone: **API methods → Allow list**, add `host.get`, `problem.get`, `item.get`, `trend.get`. `apiinfo.version` needs no permission. **Add**, then set this role on `forgesre-ro` instead of *User role*. Do not edit the built-in *User role* — other users share it.

### 3.3 API token

1. **Users → API tokens → Create API token** (6.0: **Administration → General → API tokens**).
2. **Name:** `forgesre`
3. **User:** click **Select**, pick `forgesre-ro`.
4. **Set expiration date and time:** as your policy says. When it expires, the Health cube turns yellow with `API error` — create a new token and repeat section 4.
5. **Enabled:** checked.
6. Click **Add**.
7. Copy the **Auth token** now. Zabbix shows it **once**. Click **Close**.

### 3.4 Hosts with the management IPs ForgeSRE will match

ForgeSRE links a Zabbix host to an asset by **Zabbix host ID**, then **IP**, then **hostname / asset ID**. The IP it uses is the **main interface** (Zabbix agent first, then any other interface). Loopback (`127.x`, `::1`) is ignored for matching.

On the Zabbix server:

1. **Data collection → Hosts** (Zabbix 6.4 / 7.0) or **Configuration → Hosts** (6.0 / 5.4).
2. Every host you want in ForgeSRE must exist, be **Enabled**, and sit in a host group that `ForgeSRE read-only` can **Read** (step 3.1).
3. Open the host → **Interfaces**:
   - Prefer an **Agent** interface whose **IP address** is the same management IP you already have (or will type) on the ForgeSRE asset.
   - **Connect to:** IP. If the host is DNS-only (`Connect to: DNS` and IP empty), import still works but the asset **IP** stays empty until you edit it; the webhook can still match by `{HOST.HOST}` / `{HOST.ID}` after a sync.
   - Do not use `127.0.0.1` unless this really is the Zabbix server itself — and then leave `Zabbix servers` off the read-only group ([troubleshooting](#duplicate-or-odd-hosts)).
4. **Templates** tab: attach the OS template you already use (next step) if you want Dashboard graphs from Zabbix trends.

Existing ForgeSRE assets (NetBox, Discovery, manual) are **linked**, not cloned, when the IP or hostname matches. Put the same IP on both sides **before** the first **Sync hosts**.

### 3.5 Templates / items ForgeSRE graphs actually read

Dashboard **Host metrics** and the graphs on an incident / asset page use Zabbix **only when Prometheus has no samples** for that asset ([section 11](#111-dashboard-and-incident-graphs)). The API calls are `item.get` then `trend.get` (hourly average, last 24 h). ForgeSRE looks for these item keys (first match wins):

| Graph | Item key (Zabbix) |
|---|---|
| CPU | `system.cpu.util` (or `system.cpu.util[…]` except keys containing `idle`) |
| Memory | `vm.memory.utilization` or `vm.memory.util`, else `vm.memory.size[pused…]` |
| Disk | `vfs.fs…pused` preferring `/` or `C:` |

Those keys ship with Zabbix’s stock agent templates. Names vary by version:

| Typical template | Zabbix 6.4 / 7.x name examples |
|---|---|
| Linux | **Linux by Zabbix agent** / **Linux by Zabbix agent active** (older: *Template OS Linux by Zabbix agent*) |
| Windows | **Windows by Zabbix agent** / **Windows by Zabbix agent active** |

Click path: host → **Templates** → **Select** → attach the OS template → **Update**. Confirm under **Data collection → Items** (6.0: **Configuration → Hosts → Items**) that the keys above exist, are **Enabled**, numeric, and that **Trends** are stored (Zabbix default). Trends are hourly — a brand-new host needs **about an hour** before the graph has points.

SNMP-only / agentless hosts without those keys get **no** Zabbix CPU/memory/disk graphs. That is expected. You can still get incidents from their triggers.

---

## 4. ForgeSRE: secrets and update

**There is no Admin UI field for the Zabbix URL or tokens.** Do not look under **Administration** (`/admin`) — that page is users, platform backup, and a CLI cheat sheet. Paste the three keys in **`secrets/secrets.env`** (plaintext). Optional non-secret URL override goes in **`config/forgesre.yml`**. Core picks them up at process start, so you must run `./forgesre update` (not `./forgesre restart`) the first time.

On the ForgeSRE VM, in the repo directory:

1. Generate the webhook token:

   ```bash
   openssl rand -hex 24
   ```

2. Edit `secrets/secrets.env` (plaintext, never committed; the template is `secrets/secrets.example.env` under `# --- Zabbix ---`):

   ```bash
   ZABBIX_URL=https://zabbix.example.local/zabbix   # frontend base; /api_jsonrpc.php is appended
   ZABBIX_API_TOKEN=<token from step 3.3>
   ZABBIX_WEBHOOK_TOKEN=<output of openssl rand -hex 24>
   ```

   | Key | Meaning |
   |---|---|
   | `ZABBIX_URL` | The Zabbix frontend URL you open in the browser, without `/zabbix.php…`. `https://zbx.example/zabbix` and `https://zbx.example/zabbix/api_jsonrpc.php` both work. |
   | `ZABBIX_API_TOKEN` | Token of `forgesre-ro`. With `ZABBIX_URL` it turns the integration **on**. |
   | `ZABBIX_WEBHOOK_TOKEN` | Bearer token the Zabbix media type sends to ForgeSRE. **Separate** from `ALERTMANAGER_WEBHOOK_TOKEN`. Empty = webhook answers **503** and there is no backup `problem.get` poll. |

   `ZABBIX_URL` + `ZABBIX_API_TOKEN` without `ZABBIX_WEBHOOK_TOKEN` gives you host import and graphs but no incidents.

   Do **not** put these keys in `.env`. `.env` already has `FORGESRE_HTTP_PORT` (Core listen port, default `8080`) — that is the port in the webhook URL, not a Zabbix setting. `ALERTMANAGER_WEBHOOK_TOKEN` (Prometheus path) is a **different** secret; the Zabbix media type must not send it.

3. Apply:

   ```bash
   ./forgesre update
   ```

   **Never** `./install.sh` on a running box (it regenerates secrets). `./forgesre restart` is not enough either when you changed secrets for the first time — use `update`.

4. Check: **System Health** (`/health-ui`, same as `./forgesre doctor`) → cube **Zabbix**:

   | Light | Label | Meaning |
   |---|---|---|
   | green | **Connected** | API answered and the token sees ≥ 1 host. |
   | yellow | **No hosts visible** | Token works, but `forgesre-ro` has Read on no host group (step 3.1). |
   | yellow | **Unreachable** | ForgeSRE cannot reach `ZABBIX_URL` (DNS, firewall, TLS, Zabbix down). |
   | yellow | **API error** | Zabbix answered with an error: wrong / expired token, wrong URL, no permission. |
   | grey | **Not configured** | `ZABBIX_URL` or `ZABBIX_API_TOKEN` is empty (or `inventory.zabbix.enabled: false`). |

   The cube's **GUI** link opens `ZABBIX_URL` in a new tab. A Zabbix timeout or outage is **yellow, never red** — Core keeps serving the UI.

   The **Discovery** Zabbix chip uses the same probe but a slightly different colour for failures: **grey** on that card for Unreachable / API error, **yellow** for No hosts visible. Trust the **Health** cube for “is Zabbix hurting us?” (it is not: yellow = optional source). **Sync hosts** always talks to Zabbix immediately and prints the real error — use it when the cube still shows a cached failure.

---

## 5. ForgeSRE: Discovery → Sync hosts

1. Log in to ForgeSRE as **admin** (`http://<FORGE-IP>:8080`).
2. Open **Discovery** (`/discovery`).
3. Find the **Zabbix** card. It shows the status chip (same light as the Health cube), `URL: …`, `manual (inventory.zabbix.auto_sync: false)`, and `webhook on` / `webhook off` (`on` = `ZABBIX_WEBHOOK_TOKEN` is set).
4. Click **Sync hosts**.
5. The page reloads with a notice:

   ```text
   Zabbix sync: 42 new, 3 linked, 0 skipped. New hosts are Auto type with no scrape — set Type / owner email on Assets.
   ```

   The card then shows `Last sync: <time> — Zabbix sync: 42 new, 3 linked, 0 skipped`, or `Last error: …`. Console (`/journal`) gets one `zabbix` / `sync` line per run.

### What a new Zabbix asset looks like

| Field | Value after import |
|---|---|
| Asset ID | Zabbix **technical host name** (`{HOST.HOST}`), lowercased, only `a-z0-9-`, max 63 chars (`APP-01.acme` → `app-01-acme`). Taken → `-2`, `-3`… |
| Hostname | Zabbix technical host name, unchanged |
| IP | IP of the main interface (agent first). Empty if the host only has DNS. |
| Type | **Auto (detect exporter)** |
| Scrape address | **empty** — ForgeSRE does not guess `:9100` / `:9182` |
| Environment | `Production` |
| Status | `healthy` if the host is monitored in Zabbix, `offline` if disabled |
| Source | `zabbix` (Assets **Source** filter **Zabbix**) |
| Zabbix host ID | stored; shown on the asset page with a **Zabbix agent: up / down / unknown** pill |
| Owner, owner email, phone | **empty** — fill them (section 6) |
| Customer / Site extras | first non-default host group (`Linux servers`, `Discovered hosts`, `Zabbix servers`, `Templates…` etc. are ignored). `ACME/BG-DC1` → Customer `ACME`, Site `BG-DC1`. |
| Notes | `Imported from Zabbix (visible name=…; dns=…; groups=…). Type is Auto and nothing is scraped yet — use Detect or Edit to pick the exporter.` |

Because the scrape address is empty, a Zabbix-only asset is **not** in Prometheus HTTP SD. It gets incidents from Zabbix and graphs from Zabbix trends, nothing from Prometheus — until you install an exporter and set a scrape target ([operator handbook §7](operator-handbook.md#7-making-a-server-actually-monitored)).

### Hosts ForgeSRE already knows

Existing assets are matched in this order: **Zabbix host ID**, then **IP** (not `127.x`), then **hostname / asset ID** (case-insensitive). A match is **linked**, not cloned. Linking only fills empty fields: Zabbix host ID, IP, source, Customer / Site, agent state. Owner, contact, email, phone, notes, and anything an operator typed are **never** overwritten — on any later sync either.

**Skipped** = a second Zabbix host with the IP or name of an asset that is already linked to a **different** Zabbix host ID. ForgeSRE does not guess which one is right.

Re-running **Sync hosts** is safe. Removing an asset in ForgeSRE does not touch Zabbix; the next sync imports it again unless you remove the host from the `ForgeSRE read-only` host groups.

---

## 6. ForgeSRE: owner email and NOC fields on imported assets

Escalation mail goes to the asset's **Owner email**. Imported assets have none, so until you fill it, every escalation step for a Zabbix incident is recorded as **no-recipient** and nothing is sent ([operator handbook §11](operator-handbook.md#11-escalation-and-email)).

1. **Assets** → chip **Zabbix** (or open `/assets?source=zabbix&flag=no-email` for the Zabbix assets that still have no owner email).
2. Click an asset → **Edit, clone, or remove** → **Edit**.
3. Fill at least:
   - **Owner / team** (e.g. `noc-acme`)
   - **Owner email** — the only address escalation (`team`, `team-lead`, `engineer` steps) mails
   - **Contact name**, **Owner phone**
4. NOC fields shown on the incident **Who to call** card and in the escalation mail (never mailed automatically):
   - **Backup on-call name / phone / email**
   - **Support hours**, **Timezone**
   - **License / contract / SLA**
   - **Runbook note** (for the human woken up at 3 am)
   - **Support coverage** (Under support, From / To)
5. Optional: **Type** (Linux Server, Windows Server, Network device…) if you plan to add a Prometheus exporter later. Leave **Scrape address** empty until an exporter really answers.
6. **Save**.

The Assets pill **No owner email** and the filter `flag=no-email` list what is still missing.

---

## 7. Zabbix: webhook media type

1. **Alerts → Media types → Create media type** (6.0: **Administration → Media types**).
2. **Media type** tab:
   - **Name:** `ForgeSRE`
   - **Type:** **Webhook**
   - **Parameters:** delete the default rows Zabbix adds, then **Add** each row below (name → value):

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

     The short alias `http://<FORGE-IP>:8080/webhooks/zabbix` also works. If you renamed severities in Zabbix (Administration → General → Trigger displaying options), use `{EVENT.NSEVERITY}` for `event_severity` — ForgeSRE maps the numbers 0–5 as well as the default names.

     **Which macros must be non-empty** (after Zabbix expands them). ForgeSRE treats a leftover `{MACRO}` as empty, same as a blank:

     | Must be non-empty | Macros | Else |
     |---|---|---|
     | Trigger / event **name** | `{TRIGGER.NAME}` **or** `{EVENT.NAME}` | HTTP **422** `missing trigger_name ({TRIGGER.NAME}) or event_name ({EVENT.NAME})` |
     | Host | `{HOST.HOST}` **or** `{HOST.IP}` | HTTP **422** `missing host ({HOST.HOST}) or host_ip ({HOST.IP})` |
     | Problem vs recovery | `{EVENT.VALUE}` (`1` / `0`) | Without it, `{EVENT.STATUS}` `RESOLVED` / `OK` still recovers; a missing both defaults to a **problem** |
     | Stable incident key | `{TRIGGER.ID}` + `{HOST.HOST}` | Recovery may open a second incident instead of closing the first. Fingerprint is `zabbix:{TRIGGER.ID}:{HOST.HOST}` |
     | Asset link | `{HOST.ID}` (then host, then IP) | Incident opens with **asset unknown** until you sync |

     The **Test** button in Zabbix does **not** expand macros. Leaving the values as `{TRIGGER.NAME}` is the usual **422** (token is fine — a bad token is **401** first). Overwrite those four fields for a 200; see [section 9.2](#92-from-the-zabbix-ui-media-type-test).

   - **Script:** click the pencil, paste exactly this, **Apply**:

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

     It sends every parameter except `forge_url` / `forge_token` as one flat JSON object, with `forge_token` as the Bearer token. Any non-2xx answer fails the alert in Zabbix with ForgeSRE's error text, so a wrong token or URL is visible in **Reports → Action log**.

   - **Timeout:** `10s`
   - **Process tags**, **Include event menu entry:** unchecked.
   - **Enabled:** checked.
3. **Message templates** tab → **Add** → **Message type: Problem** → **Add**. Again **Add** → **Problem recovery** → **Add**. ForgeSRE does not use the text; Zabbix only needs a template for each message type it sends.
4. **Options** tab: defaults are fine (concurrent sessions *One*, attempts `3`, interval `10s`).
5. Click **Add**.

---

## 8. Zabbix: media on the user and the trigger action

### 8.1 Media on `forgesre-ro`

1. **Users → Users → `forgesre-ro`** → **Media** tab → **Add**.
2. **Type:** `ForgeSRE`
3. **Send to:** `forgesre` (any non-empty text; the webhook does not use it)
4. **When active:** `1-7,00:00-24:00`
5. **Use if severity:** tick all.
6. **Enabled:** checked → **Add**, then **Update** on the user.

### 8.2 Trigger action

1. **Alerts → Actions → Trigger actions → Create action** (6.0: **Configuration → Actions → Trigger actions**).
2. **Action** tab:
   - **Name:** `ForgeSRE`
   - **Conditions:** optional. Examples: *Host group equals Production*, *Trigger severity is greater than or equals Warning*. No condition = every problem on hosts `forgesre-ro` can read.
   - **Enabled:** checked.
3. **Operations** tab:
   - **Operations → Add:** **Send to users** → `forgesre-ro`; **Send only to** → `ForgeSRE`. Step duration **1 – 1** (first step only, immediately). **Add**.
   - **Recovery operations → Add:** **Send to users** → `forgesre-ro`, **Send only to** → `ForgeSRE` (or operation *Notify all involved*). **Add**. Recovery is what sets the ForgeSRE incident to **RESOLVED**.
   - **Update operations:** leave empty. ForgeSRE ignores acknowledges and comments made in Zabbix (no write-back the other way either).
4. Click **Add**.

Zabbix 7.x labels the same tabs **Operations** / **Recovery operations** / **Update operations**. 6.0 uses **Configuration → Actions**. The operation type is **Send message** / **Send to users** depending on version — pick the ForgeSRE media, not Email.

Without the **Recovery operation**, incidents are still resolved by the backup `problem.get` poll (every 3 minutes, incidents older than 2 minutes), but later and less reliably. Always add it.

---

## 9. Test the webhook

Every test that returns 200 with a problem payload **opens a real incident** in ForgeSRE (with an immediate escalation step). Use a test trigger name and close it with the matching recovery right after.

### 9.1 From the Zabbix server shell (network + token)

```bash
FORGE=http://<FORGE-IP>:8080
TOKEN=<ZABBIX_WEBHOOK_TOKEN>

# problem → expect HTTP 200 and an incident number
curl -sS -X POST "$FORGE/api/v1/webhooks/zabbix" \
  -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' \
  -d '{"event_value":"1","event_status":"PROBLEM","event_severity":"Warning",
       "trigger_id":"999001","trigger_name":"ForgeSRE webhook test","host_host":"zabbix-webhook-test"}'
# {"accepted":true,"source":"zabbix","status":["firing"],"incidents":["INC-0141_08.10.2026_07:52"]}

# recovery → same trigger_id + host_host, event_value 0 → incident RESOLVED
curl -sS -X POST "$FORGE/api/v1/webhooks/zabbix" \
  -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' \
  -d '{"event_value":"0","event_status":"RESOLVED","event_severity":"Warning",
       "trigger_id":"999001","trigger_name":"ForgeSRE webhook test","host_host":"zabbix-webhook-test"}'
# {"accepted":true,"source":"zabbix","status":["resolved"],"incidents":[]}
```

| Answer | Meaning |
|---|---|
| **200** `"accepted": true` | URL and token are right. |
| **401** `invalid zabbix webhook token` | `forge_token` / `$TOKEN` differs from `ZABBIX_WEBHOOK_TOKEN` (watch for spaces or a trailing newline). |
| **503** `Zabbix webhook is off: set ZABBIX_WEBHOOK_TOKEN…` | The token is empty on ForgeSRE, or you did not run `./forgesre update` after setting it. |
| **422** `missing trigger_name ({TRIGGER.NAME}) or event_name ({EVENT.NAME})` / `missing host ({HOST.HOST}) or host_ip ({HOST.IP})` | Token is right; the body lacks the trigger name or the host. |
| connection refused / timeout | Firewall between the Zabbix server and ForgeSRE TCP 8080, or wrong IP. |

### 9.2 From the Zabbix UI (media type Test)

1. **Alerts → Media types** → row `ForgeSRE` → **Test** (Actions column).
2. The dialog shows each parameter with its macro, e.g. `{TRIGGER.NAME}`. Macros are **not** expanded in a test, and ForgeSRE treats an unexpanded macro as empty, so:
   - Left as macros → **`ForgeSRE HTTP 422: … missing trigger_name …`**. That already proves the URL and token are right (a wrong token would be 401).
   - For a full **200**, overwrite at least `event_value` = `1`, `trigger_id` = `999001`, `trigger_name` = `ForgeSRE webhook test`, `host_host` = `zabbix-webhook-test`, then **Test**. Expected: *Media type test successful*, response `OK`.
3. Run **Test** again with `event_value` = `0` to resolve that test incident.

### 9.3 End to end

Pick a harmless trigger on a test host (or temporarily lower a threshold), let it fire, and check:

- **Incidents** (`/incidents`): a new row with the **Zabbix** pill, title = `{EVENT.NAME}`, asset = the imported host.
- Timeline: `<trigger name> fired in Zabbix`, then `INC-… created`.
- When Zabbix recovers the problem: status **RESOLVED**, timeline `<trigger name> resolved by Zabbix`.
- **Reports → Action log** in Zabbix: the `ForgeSRE` media shows *Sent*.

---

## 10. ForgeSRE: playrules for Zabbix triggers

A playrule attaches a playbook, severity, and escalation policy to an incident by **alertname**. For Zabbix, alertname = the trigger name (`{TRIGGER.NAME}`, or `{EVENT.NAME}` when the trigger name is empty).

1. In Zabbix **Monitoring → Problems** (or the trigger list), copy the trigger name **as shown**. Macros are expanded: a trigger defined as `Load on {HOST.NAME}` arrives as `Load on app-01`.
2. ForgeSRE **Playrules** (`/playrules`, analyst or admin) → **Create playrule**.
3. **Name:** anything unique, e.g. `zbx-high-cpu`.
4. **Alertname:** paste the exact trigger name, e.g. `High CPU utilization (over 90% for 5m)`. Matching is exact and case-insensitive; no wildcards.
5. Severity, **Playbook**, **Escalation policy** as you like → **Save**.

The playrule list shows **No Prometheus rule** next to Zabbix alertnames, and the form preview says *none in alerts.yml for this alertname* / *Never matches until alerts.yml has this alertname*. That pill is leftover Prometheus wording. **It is wrong for Zabbix:** matching is **Alertname after ingest**, not PromQL, and Grafana is not involved. A Zabbix playrule with no row in `monitoring/alerts.yml` still attaches its playbook and escalation to Zabbix incidents.

A trigger name that contains a host name (`Load on app-01`) needs one playrule per host — prefer trigger names without host macros if you want one rule for all hosts.

**Name** vs **Alertname** on the form:

| Field | What it is | What it is not |
|---|---|---|
| **Name** | Unique label in the list (`zbx-high-cpu`) | Not matched on ingest |
| **Alertname** | The only match key. Must equal the Zabbix trigger/event name the webhook sent | Not PromQL. Not Grafana. Not the playrule Name |
| Metric / operator / value | Operator note, stored, **never evaluated** | Not a Zabbix trigger expression |

**Client playrules** on the asset (Add/Edit, right column, or the asset page card) are tried first, in saved order. The first enabled one whose Alertname equals this trigger wins. Empty list → the global Playrules list (first enabled by id).

Severity of the incident comes from Zabbix (section 14), not from the playrule, when Zabbix sends one. The playrule severity is the fallback only when `event_severity` is missing.

---

## 11. Where Zabbix data shows up in ForgeSRE

| Where | What you see |
|---|---|
| **Assets** (`/assets`) | Source chips **Zabbix / agent up / agent down / agent unknown** (`/assets?source=zabbix`, `&agent=down`). `source=zabbix` also includes NetBox / Discovery / manual assets linked to a Zabbix host. Each row has a **Zabbix agent: …** pill. |
| **Asset page** | **Zabbix host ID** with the agent pill; notes from the import. |
| **Discovery** (`/discovery`) | Zabbix card: status, URL, auto / manual, webhook on / off, last sync or last error, **Sync hosts**. |
| **Incidents** (`/incidents`) and the incident page | **Zabbix** pill next to the incident (Prometheus incidents show **Prometheus**). Same lifecycle: playrule, playbook, Who to call, escalation, ForgeRCA. |
| Escalation / incident mail | **Zabbix** source in the mail header. |
| **Host metrics** (incident page, Dashboard) | See [§11.1](#111-dashboard-and-incident-graphs). |
| **System Health** (`/health-ui`) | Cube **Zabbix**: green / yellow / grey, GUI link to `ZABBIX_URL`. Timeout / outage = **yellow**, never red. |
| **Console** (`/journal`) | Module `zabbix`: `sync`, `health` (down / back), `poll` (incident resolved by the backup poll). Incidents: `incident` / `create` with `source=zabbix`. Same filter from the CLI: `./forgesre journal zabbix`. |

### 11.1 Dashboard and incident graphs

ForgeSRE draws CPU / memory / disk on **Dashboard → Host metrics** (click a recent-incident row) and on the incident / asset page. Grafana is **not** this path.

Order of sources (per asset, every ~30 s refresh, Zabbix trends cached 5 minutes):

1. **Prometheus `query_range`** — used when the asset has a scrape address and Prometheus already has samples (`up` or a CPU/memory/disk series). This is the bundled exporter path (node_exporter `:9100`, windows_exporter `:9182`, snmp_exporter).
2. **Zabbix `item.get` + `trend.get` fallback** — used only when the asset has a **Zabbix host ID** **and** Prometheus has **no** samples. Caption: *Zabbix trends (hourly average, last 24 h). No Prometheus samples for this host.*
3. Empty / *No Zabbix CPU / memory / disk items on this host.* — no matching keys (section 3.5), items disabled, or trends not stored yet.
4. *Zabbix unavailable — …* — API call failed; see the Health cube.

What you must have for Zabbix graphs to appear:

- `ZABBIX_URL` + `ZABBIX_API_TOKEN` set, Health cube not “Not configured”.
- The asset linked (`zabbix_hostid` on the asset page). Run **Sync hosts** if the pill is missing.
- **Scrape address empty** (or Prometheus not yet collecting). The moment Prometheus has samples, graphs **switch to Prometheus** and stay there.
- Stock agent items from section 3.5, with at least an hour of trends.

Zabbix-only assets (import default) use (2). Dual-homed assets (exporter + Zabbix) use (1). You do not configure PromQL for Zabbix graphs.

---

## 12. Optional: automatic host import

Off by default. To import with the existing discovery loop (about every 6 h), in `config/forgesre.yml`:

```yaml
inventory:
  zabbix:
    auto_sync: true
    timeout_seconds: 4   # per API call, clamped 2–5
    # enabled: false     # force off even if secrets are set
    # url: "https://zabbix.example.local/zabbix"   # overrides ZABBIX_URL
```

Then `./forgesre update`. The Discovery card then says `auto every 6 h`.

---

## 13. Troubleshooting

### Health cube Zabbix is yellow

Read the text under the cube (and **Last error** on the Discovery card):

| Text | Fix |
|---|---|
| `No hosts visible: Token works but sees 0 hosts…` | Step 3.1: add host groups with **Read** to `ForgeSRE read-only`, and check `forgesre-ro` is in that group. |
| `Unreachable: Zabbix unreachable: …` | From the Forge VM run the `apiinfo.version` curl from section 2. DNS, firewall, proxy, or TLS certificate. Zabbix down is expected to be yellow; ForgeSRE keeps working. |
| `API error: Zabbix API: …` mentioning *not authorized* or *session terminated* | Wrong, disabled, or expired token, a disabled user, or a role without API access. New token (3.3) → `secrets/secrets.env` → `./forgesre update`. |
| `API error: … No permissions to call "host.get"` | You used a restricted role without `host.get` in the API allow list (optional step in 3.2). |
| `API error: Zabbix HTTP 404 from … (check ZABBIX_URL)` / `answered non-JSON` | `ZABBIX_URL` points at the wrong path. Use the frontend base (`…/zabbix`) where `api_jsonrpc.php` lives. |

The light is cached (1 minute when OK, 2 minutes after a failure), and after a connection failure ForgeSRE waits 2 minutes before calling Zabbix again. **Run doctor** does not skip that wait. **Sync hosts** does — it calls Zabbix immediately and shows the real error in the notice. `./forgesre update` also clears it.

### Webhook 401 / 503 / 422

See the table in [section 9.1](#91-from-the-zabbix-server-shell-network--token). In Zabbix, a failed send shows in **Reports → Action log** as `ForgeSRE HTTP 401: …`. Most common: `forge_token` has a leading/trailing space, `ZABBIX_WEBHOOK_TOKEN` was added without `./forgesre update`, or `forge_url` uses `localhost` (that is the Zabbix server itself).

### Incidents open but never resolve

- The action has no **Recovery operation** (8.2). The backup poll resolves them within ~3 minutes only if `ZABBIX_WEBHOOK_TOKEN` is set and the API token can `problem.get` the trigger.
- The `trigger_id` or `host_host` parameter is missing or different between problem and recovery. The incident key is `zabbix:{TRIGGER.ID}:{HOST.HOST}`.

### Incident has no asset ("asset unknown")

The webhook matches the asset by Zabbix host ID (`host_id`), then `host_host` as asset ID / hostname, then `host_ip`. Run **Sync hosts** so the host exists as an asset, and keep the `host_id` parameter in the media type.

### Duplicate or odd hosts

- **Same server twice** (one manual / NetBox asset, one `source=zabbix`): the existing asset had a different IP and hostname than Zabbix when you synced. Fix the IP or hostname on the original asset, **remove** the Zabbix copy, and **Sync hosts** again — the host is then linked to the original by IP or name.
- **`skipped` in the sync notice**: two Zabbix hosts share an IP or name, and one is already linked. Clean it up in Zabbix (one host per server) or leave the second one out of the host groups.
- **`zabbix-server` with IP `127.0.0.1`**: the `Zabbix servers` host group is readable by `forgesre-ro`. Remove that group from the user group, or edit the asset's IP.
- Template hosts and lab/demo assets are never imported or matched.

### Scrape still empty / no Prometheus graphs

Expected. Zabbix import never sets a scrape address, and ForgeSRE does not install exporters. Zabbix-only assets get graphs from Zabbix trends instead. When you want Prometheus as well: install node_exporter / windows_exporter (or set SNMP for network devices), then **Detect OS / scrape port** or **Edit → Scrape address** ([operator handbook §7](operator-handbook.md#7-making-a-server-actually-monitored)). Once Prometheus has samples, the graphs switch to Prometheus automatically.

### Graphs say "No Zabbix CPU / memory / disk items on this host" or stay empty

The host has no `system.cpu.util`, `vm.memory.utilization` / `vm.memory.size[pused]`, or `vfs.fs…[…,pused]` items, the items are disabled, or trends are not stored yet (trends are hourly; a new host needs at least an hour). `Zabbix unavailable — …` means the API call failed; see the Health cube.

### Agent pill always `unknown`

The host has no Zabbix agent interface (SNMP-only, or agentless), or Zabbix has not checked it yet. The pill refreshes at most every 5 minutes.

### Playrule does not match

The **Alertname** must equal the trigger name exactly as it arrives (expanded macros, same spaces and punctuation; case does not matter). Open the incident → the title and timeline show the name ForgeSRE received. The list pill **No Prometheus rule** does **not** mean the playrule is dead — it only means `alerts.yml` has no PromQL with that name.

### TLS / wrong path / connection reset

- `ZABBIX_URL` with a private CA: Core (host network) uses the VM's trust store. Install the CA on Ubuntu (`/usr/local/share/ca-certificates` + `update-ca-certificates`) or use HTTP on the management LAN.
- Certificate hostname must match the URL. A `https://10.x` URL with a name-only cert fails — use the name in `ZABBIX_URL`.
- Media type `forge_url` must be the Forge **Core** URL (`/api/v1/webhooks/zabbix` or `/webhooks/zabbix`), not Grafana `:3000`, not Prometheus `:9090`, not `/api/v1/webhooks/alertmanager`.
- HTTPS on Core: `./forgesre tls` then put that origin in `forge_url`. HTTP `:8080` will fail if you redirected everything to TLS.

### Host IP mismatch so the asset is not linked

Sync matches **Zabbix host ID**, then **IP** (not loopback), then hostname / asset ID. Webhook matches **host_id**, then `host_host` as asset ID / hostname, then `host_ip`. If Zabbix has `10.20.1.15` and the ForgeSRE asset has `10.20.1.16`, you get a second asset on sync (or an incident with **asset unknown**). Fix the IP on one side, remove the duplicate, **Sync hosts** again. Keep `{HOST.ID}` in the media type.

### Standard alarms “muted” but Zabbix incidents still open

Expected. Standard alarms only overlay bundled Prometheus alertnames. Unchecking CPU does not mute a Zabbix trigger unless that trigger's **name** is exactly `HighCPU` / `NodeCPUHigh` / `WindowsCPUHigh` ([section 15](#15-standard-alarms-vs-zabbix-incidents)).

---

## 14. Reference: payload and mapping

Body the media type script sends (keys are case- and punctuation-insensitive: `EVENT.VALUE`, `event_value`, `eventValue` are the same field; an unexpanded macro like `{HOST.IP}` counts as empty). A JSON array of up to 50 such objects is also accepted.

```json
{
  "event_value": "1",
  "event_status": "PROBLEM",
  "event_id": "81234",
  "event_name": "High CPU utilization (over 90% for 5m)",
  "event_severity": "High",
  "event_opdata": "Current utilization: 97 %",
  "event_date": "2026.10.08",
  "event_time": "07:52:10",
  "trigger_id": "23456",
  "trigger_name": "High CPU utilization (over 90% for 5m)",
  "host_host": "app-01",
  "host_name": "app-01 (prod)",
  "host_ip": "10.20.1.15",
  "host_id": "10584"
}
```

Required: `trigger_name` (or `event_name`) **and** `host_host` (or `host_ip`). Everything else is optional.

| Zabbix | ForgeSRE |
|---|---|
| `event_value` `1` | problem → opens the incident (or keeps the open one) |
| `event_value` `0` (or `event_status` `RESOLVED` / `OK` when `event_value` is absent) | recovery → incident **RESOLVED**, timeline *resolved by Zabbix* |
| `trigger_name`, else `event_name` | `alertname` (playrule match) |
| `event_name`, else `trigger_name` | incident title |
| `event_opdata` | incident summary |
| Severity **Disaster**, **High** (or 5, 4) | **CRITICAL** |
| Severity **Average**, **Warning** (or 3, 2) | **WARNING** |
| Severity **Information**, **Not classified** (or 1, 0) | **INFO** |
| Unknown severity text | **WARNING** |
| `trigger_id` + `host_host` | fingerprint `zabbix:{TRIGGER.ID}:{HOST.HOST}` — one active incident per trigger and host; the recovery closes the one it opened |
| `host_id`, then `host_host`, then `host_ip` | asset match |
| all of the above | stored as labels (`source=zabbix`, `zabbix_hostid`, `zabbix_triggerid`, `zabbix_eventid`, `zabbix_severity`, `zabbix_host_name`, `ip`) on the incident |

The same problem firing again after its incident was **RESOLVED** opens a new incident (fresh escalation) linked to the previous one, exactly like Prometheus alerts.

Incident IDs look like `INC-0141_08.10.2026_07:52` (sequence + local date/time). Older `INC-000012` rows stay valid.

See also: [operator handbook §18](operator-handbook.md#18-zabbix-read-only-source) (summary), [§8 Alerts become incidents](operator-handbook.md#8-alerts-become-incidents), [§9 Playrules](operator-handbook.md#9-playrules), [§11 Escalation and email](operator-handbook.md#11-escalation-and-email), [§12 Incident workflow](operator-handbook.md#12-incident-workflow).

---

## 15. Standard alarms vs Zabbix incidents

ForgeSRE has two alarm paths. Do not mix the mute switches.

| | Bundled / “standard” alarms | Zabbix-originated incidents |
|---|---|---|
| Origin | Prometheus rules in `monitoring/alerts.yml` (`HighCPU`, `NodeCPUHigh`, `NodeExporterDown`, `WindowsCPUHigh`, `SnmpDeviceUnreachable`, …) → Alertmanager → `POST /api/v1/webhooks/alertmanager` | Zabbix trigger → media type → `POST /api/v1/webhooks/zabbix` |
| Pill on the incident | **Prometheus** | **Zabbix** |
| Playrule **Alertname** | Exact Prometheus `alertname` | Exact Zabbix `{TRIGGER.NAME}` (or `{EVENT.NAME}`) |
| Asset **Standard alarms** (Add/Edit, right column: Collecting / CPU / Memory / Disk enable + %) | **Mute-only overlay.** Unchecking CPU skips opening an incident for `HighCPU` / `NodeCPUHigh` / `WindowsCPUHigh` on that host. It cannot fire earlier than `alerts.yml`. | **Does not mute Zabbix triggers.** A Zabbix “CPU > 90%” problem still opens an incident. |
| Grafana | Not in the path | Not in the path |

**Exception:** `bundled_alert_skip_reason` keys off the **alertname string**. If you name a Zabbix trigger exactly `HighCPU`, `NodeCPUHigh`, `FilesystemUsageHigh`, `NodeExporterDown`, and so on, unchecking the matching Standard alarm **will** skip that Zabbix incident. Do not reuse those Prometheus names for Zabbix triggers.

Mute in Zabbix (maintenance, disabled trigger, severity filter on the action) is the only way to stop Zabbix problems reaching ForgeSRE. ForgeSRE never disables a Zabbix trigger.

---

## 16. Acknowledge, resolve, close — no write-back

On the incident page (`/incidents/INC-…`):

| Button | ForgeSRE status | What happens in Zabbix |
|---|---|---|
| **Acknowledge** | `INVESTIGATING` (an `ESCALATED` incident stays `ESCALATED`; ack time is recorded) | Nothing. ForgeSRE does not call `event.acknowledge`. |
| **Resolve** | `RESOLVED` (human) | Nothing. The Zabbix problem stays PROBLEM until Zabbix recovers it. |
| **Close** | `CLOSED` (human archive) | Nothing. Close is ForgeSRE-only. `CLOSED` is final — a later fire opens a **new** INC. |

When Zabbix recovers the problem (recovery operation webhook, or the 3-minute `problem.get` backup poll):

- Active incident (`OPEN` / `INVESTIGATING` / `ESCALATED`) becomes **RESOLVED**.
- Timeline: `<trigger name> resolved by Zabbix`.
- ForgeSRE does **not** set `CLOSED`. Close is still a person.

The other direction is also one-way:

- An acknowledge or comment **in Zabbix** is ignored (leave **Update operations** empty on the action).
- Resolving or closing in ForgeSRE does **not** recover the Zabbix problem. If the trigger is still PROBLEM, Zabbix will not send a new problem webhook until it recovers and fires again. The ForgeSRE incident you resolved stays resolved; a later fire after Zabbix recovery opens a new INC (RE-FIRED link).

Escalation mail still goes to the asset **Owner email**. Mute of Standard alarms never applies here.

---

## 17. CLI commands that matter

There is **no** `./forgesre zabbix`. Use the real wrapper commands from [cli.md](cli.md):

```bash
./forgesre doctor              # Health lights, including component zabbix (yellow = optional source)
./forgesre status              # docker compose ps — confirm core is up
./forgesre journal zabbix      # Console lines: sync / health / poll
./forgesre incidents           # Board; open one with ./forgesre incidents INC-0141_08.10.2026_07:52
./forgesre config              # prints config/forgesre.yml (inventory.zabbix)
./forgesre update              # after secrets or YAML change; also after git pull of code
./forgesre restart             # bounce containers; NOT enough the first time you add ZABBIX_* keys
./forgesre tls                 # only if Core is behind HTTPS — then the media-type URL is that URL
./forgesre secrets-check       # SECRET_KEY / Alertmanager webhook defaults; not a Zabbix checker
```

`./forgesre verify` is the **Prometheus** chain (exporter → Prometheus → Alertmanager → Core). It does not test the Zabbix webhook. `./forgesre jobs` is the RCA / discovery **queue**, not the in-process Zabbix poll (that poll is inside Core every 3 / 5 minutes). `./forgesre snmp-auths` is snmp_exporter, not Zabbix.

---

## 18. End-to-end operator checklist

Do these in order the first time. After that, the short version at the top of this page is enough.

1. **ForgeSRE is up.** `./forgesre doctor` — Core green. Login `http://<FORGE-IP>:8080`. Note `FORGESRE_HTTP_PORT` in `.env` (default 8080). Never `./install.sh`.
2. **Zabbix API user.** User group `ForgeSRE read-only` with **Read** on the host groups → user `forgesre-ro` with **User role** → API token (section 3). Hosts exist with the **same management IPs** you will match; OS templates attached if you want graphs (3.4–3.5).
3. **Paste secrets.** On the Forge VM, `ZABBIX_URL`, `ZABBIX_API_TOKEN`, `ZABBIX_WEBHOOK_TOKEN` in `secrets/secrets.env` only. `./forgesre update`. Health cube **Zabbix** green **Connected** (yellow = timeout / 0 hosts / API error, never red).
4. **First sync.** **Discovery → Zabbix → Sync hosts** (admin). Notice `N new, M linked, K skipped`. **Assets → Zabbix** shows the hosts. Fill **Owner email** (and NOC fields) on new assets.
5. **Webhook + action.** Zabbix media type **ForgeSRE** posting to `http://<FORGE-IP>:8080/api/v1/webhooks/zabbix` with Bearer = `ZABBIX_WEBHOOK_TOKEN`, macros from section 7. Media on `forgesre-ro`. Trigger action with **problem + recovery** operations (section 8). Firewall: Zabbix **server** → Forge TCP 8080.
6. **Fire a test problem.** Media-type **Test** with overwritten macros (422 with raw `{TRIGGER.NAME}` means token+URL are OK; 401 is the token). Then curl from the Zabbix server, then a real trigger. Success: HTTP 200 `"accepted": true`, Zabbix **Reports → Action log** *Sent*, ForgeSRE **Incidents** a new row with the **Zabbix** pill, id `INC-NNNN_DD.MM.YYYY_HH:MM`, title = event name, asset = the imported host.
7. **Playrule.** **Playrules → Create playrule**. **Alertname** = the exact trigger name as it arrived (not the playrule Name, not PromQL). Ignore **No Prometheus rule**. Open the incident — playbook / escalation attached.
8. **Graphs.** Open the asset or click the incident on the Dashboard. Zabbix-only host: caption *Zabbix trends (hourly average, last 24 h)* once items+trends exist. Scraped host: Prometheus graphs instead.
9. **Mute / ack / resolve.** Uncheck Standard alarms CPU — a **Prometheus** `HighCPU` is skipped; a **Zabbix** CPU trigger is not. **Acknowledge** / **Resolve** / **Close** on the ForgeSRE incident do **not** change Zabbix. Recover the problem in Zabbix → incident **RESOLVED** by Zabbix. Close is human, in ForgeSRE only.

---

## 19. After git pull

Docs live in `docs/` in git. The running UI does not serve them.

| What changed | What to run on the live VM |
|---|---|
| Markdown only (`docs/zabbix.md`, handbook, …) | `git pull origin main` — read the files; no `./forgesre update` required |
| Code, templates, compose, or you edited `secrets/secrets.env` / `config/forgesre.yml` | `git pull origin main && ./forgesre update` |
| Container wedged, same config | `./forgesre restart` (does not reload new secrets on first add) |

**Never** `./install.sh` on a box that already runs ForgeSRE.
