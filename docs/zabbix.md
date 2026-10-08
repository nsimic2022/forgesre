# Connecting Zabbix to ForgeSRE

Step-by-step cookbook: every click in Zabbix and every step on the ForgeSRE VM so that Zabbix **hosts** become ForgeSRE **assets**, Zabbix **problems** become ForgeSRE **incidents**, and Zabbix **trends** show up as graphs.

Zabbix is optional. The Prometheus path (node_exporter, windows_exporter, snmp_exporter → Alertmanager → incidents) works without it and does not change when you add it.

Short version for people who have done this before:

1. Zabbix: user group with **Read** on the host groups → user `forgesre-ro` with **User role** → API token.
2. Forge VM: `ZABBIX_URL`, `ZABBIX_API_TOKEN`, `ZABBIX_WEBHOOK_TOKEN` in `secrets/secrets.env` → `./forgesre update`.
3. Forge UI: **Discovery → Zabbix → Sync hosts** → fill **Owner email** on the new assets.
4. Zabbix: media type **ForgeSRE** (Webhook, script below) → media on `forgesre-ro` → trigger action with problem + recovery operations.
5. Test the webhook (HTTP 200), then add playrules whose **alertname** is the exact Zabbix trigger name.

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

- **No write-back.** ForgeSRE only calls `apiinfo.version`, `host.get`, `problem.get`, `item.get`, `trend.get`. The client refuses any other method (`*.create`, `*.update`, `*.delete`, `event.acknowledge`) before a request is built.
- **No acknowledge, close, comment, or severity change in Zabbix.** Acknowledging or resolving an incident in ForgeSRE stays in ForgeSRE. The Zabbix problem stays as it is until Zabbix itself recovers it.
- No host, group, template, item, trigger, user, media type, or action is created or changed in Zabbix. You create those by hand (sections 3, 7, 8).
- No Zabbix iframe or embedded Zabbix UI. The Health page only links to `ZABBIX_URL`.
- No Zabbix database access, no extra container, no Zabbix proxy or agent on the ForgeSRE VM.
- No `./forgesre zabbix` CLI command. Everything is the secrets file, `./forgesre update`, and the web UI.
- No SNMP settings are needed for Zabbix. `./forgesre snmp-auths` and the asset SNMP fields are for ForgeSRE's own snmp_exporter, not for Zabbix.
- Problems are not pulled. Incidents are opened **only** by the webhook. The 3-minute `problem.get` poll can only resolve an incident whose recovery webhook was lost.

If Zabbix is down: each API call gives up after 2–5 s, the **Zabbix** Health cube turns **yellow** (never red), Console gets **one** `zabbix` / `health` line, and ForgeSRE stops calling Zabbix for 2 minutes instead of retrying. Dashboard, incidents, Prometheus alerts, and mail keep working.

---

## 2. Prerequisites

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

---

## 4. ForgeSRE: secrets and update

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

   The cube's **GUI** link opens `ZABBIX_URL` in a new tab.

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

The Dashboard tile **No owner email** and the Assets pill **No owner email** list what is still missing.

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
   - **Operations → Add:** **Send to users** → `forgesre-ro`; **Send only to** → `ForgeSRE`. Step `1 - 1`. **Add**.
   - **Recovery operations → Add:** **Send to users** → `forgesre-ro`, **Send only to** → `ForgeSRE` (or operation *Notify all involved*). **Add**.
   - **Update operations:** leave empty. ForgeSRE ignores acknowledges and comments made in Zabbix.
4. Click **Add**.

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

The playrule list shows **No Prometheus rule** next to Zabbix alertnames. That pill only says that `monitoring/alerts.yml` has no rule with that name; the playrule still matches Zabbix incidents. A trigger name that contains a host name (`Load on app-01`) needs one playrule per host — prefer trigger names without host macros if you want one rule for all hosts.

Severity of the incident comes from Zabbix (section 14), not from the playrule, when Zabbix sends one.

---

## 11. Where Zabbix data shows up in ForgeSRE

| Where | What you see |
|---|---|
| **Assets** (`/assets`) | Source chips **Zabbix / agent up / agent down / agent unknown** (`/assets?source=zabbix`, `&agent=down`). `source=zabbix` also includes NetBox / Discovery / manual assets linked to a Zabbix host. Each row has a **Zabbix agent: …** pill. |
| **Asset page** | **Zabbix host ID** with the agent pill; notes from the import. |
| **Discovery** (`/discovery`) | Zabbix card: status, URL, auto / manual, webhook on / off, last sync or last error, **Sync hosts**. |
| **Incidents** (`/incidents`) and the incident page | **Zabbix** pill next to the incident (Prometheus incidents show **Prometheus**). Same lifecycle: playrule, playbook, Who to call, escalation, ForgeRCA. |
| Escalation / incident mail | **Zabbix** source in the mail header. |
| **Host metrics** (incident page, Dashboard) | For an asset with a Zabbix host ID and **no Prometheus samples**: CPU / memory / disk for the last 24 h from `trend.get` (items `system.cpu.util`, `vm.memory.utilization`, `vfs.fs…pused`), caption *Zabbix trends (hourly average, last 24 h). No Prometheus samples for this host.* Scraped assets keep their Prometheus graphs. |
| **System Health** (`/health-ui`) | Cube **Zabbix**: green / yellow / grey, GUI link to `ZABBIX_URL`. |
| **Console** (`/journal`) | Module `zabbix`: `sync`, `health` (down / back), `poll` (incident resolved by the backup poll). Incidents: `incident` / `create` with `source=zabbix`. |

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

The **Alertname** must equal the trigger name exactly as it arrives (expanded macros, same spaces and punctuation; case does not matter). Open the incident → the title and timeline show the name ForgeSRE received.

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

See also: [operator handbook §18](operator-handbook.md#18-zabbix-read-only-source) (summary), [§8 Alerts become incidents](operator-handbook.md#8-alerts-become-incidents), [§9 Playrules](operator-handbook.md#9-playrules), [§11 Escalation and email](operator-handbook.md#11-escalation-and-email).
