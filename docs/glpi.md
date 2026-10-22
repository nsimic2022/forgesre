# Connecting GLPI to ForgeSRE

Step-by-step cookbook: every click in GLPI and every step on the ForgeSRE VM so that GLPI **Computers** and **NetworkEquipment** become ForgeSRE **assets**.

GLPI is optional. The Prometheus path (node_exporter, windows_exporter, snmp_exporter → Alertmanager → incidents) works without it and does not change when you add it. This import is **inventory only** — GLPI tickets and problems do not become ForgeSRE incidents.

Short version for people who have done this before:

1. GLPI: **Setup → General → API** → enable REST API → create an API client (App-Token).
2. GLPI: read-only user that can see Computer and NetworkEquipment → **Remote access key** (User-Token).
3. Forge VM: `GLPI_URL`, `GLPI_APP_TOKEN`, `GLPI_USER_TOKEN` in `secrets/secrets.env` → `./forgesre update`.
4. Forge UI: **Discovery → GLPI → Sync hosts** → fill **Owner email** on the new assets.

Never done this? Follow the numbered [end-to-end operator checklist](#10-end-to-end-operator-checklist) after the click-paths below.

---

## Contents

1. [What ForgeSRE does with GLPI](#1-what-forgesre-does-with-glpi)
2. [Prerequisites](#2-prerequisites)
3. [GLPI: enable the REST API and create an App-Token](#3-glpi-enable-the-rest-api-and-create-an-app-token)
4. [GLPI: read-only user and User-Token](#4-glpi-read-only-user-and-user-token)
5. [ForgeSRE: secrets and update](#5-forgesre-secrets-and-update)
6. [ForgeSRE: Discovery → Sync hosts](#6-forgesre-discovery--sync-hosts)
7. [Owner email and NOC fields on imported assets](#7-owner-email-and-noc-fields-on-imported-assets)
8. [Optional: automatic import](#8-optional-automatic-import)
9. [Troubleshooting](#9-troubleshooting)
10. [End-to-end operator checklist](#10-end-to-end-operator-checklist)
11. [After git pull](#11-after-git-pull)

---

## 1. What ForgeSRE does with GLPI

**Does:**

| Data | How | When |
|---|---|---|
| Computer / NetworkEquipment → assets | REST GET `initSession`, `Computer`, `NetworkEquipment`, `IPAddress`, `killSession` | **Discovery → Sync hosts** (admin), or every ~6 h with `auto_sync: true` |
| Status light | `initSession` + one Computer / NetworkEquipment row | Health cube **GLPI** and the Discovery GLPI card (cached 1–2 minutes) |

**Does not:**

- Write anything back to GLPI (no create / update / delete / ticket / reservation). ForgeSRE never writes back.
- Open incidents from GLPI tickets or problems. Incidents stay Prometheus → Alertmanager (and optional Zabbix webhook).
- Guess a scrape port. New assets are type **Auto** with an empty scrape address.
- Overwrite owner, contact, email, phone, notes, or already-filled extras on an asset that already exists.

**How Core talks to GLPI:**

- Base URL is `GLPI_URL` (or `inventory.glpi.url`). Core appends `/apirest.php` unless the URL already ends with it.
- Auth: `App-Token` (API client) + `Authorization: user_token …` (remote access key). A session token is requested per sync and never logged.
- Health links to `GLPI_URL`. Tokens live in `secrets/secrets.env`. There is no Administration form for those secrets.
- If GLPI is down: each API call gives up after 2–5 s, the **GLPI** Health cube turns **yellow** (never red), Console gets **one** `glpi` / `health` line, and ForgeSRE stops calling GLPI for 2 minutes instead of retrying. Dashboard, incidents, Prometheus alerts, and mail keep working.

Match order when linking an existing ForgeSRE asset: **GLPI id** (`Computer:12`), then IP, then hostname. A second GLPI item that collides with an already-linked id is skipped, not cloned.

---

## 2. Prerequisites

Two machines: the **GLPI you already run**, and the **ForgeSRE appliance** (one Ubuntu VM, Docker Compose, **host networking**).

### 2.1 Appliance is up

On the Forge VM, before touching GLPI:

```bash
cd ~/forgesre   # or wherever the clone lives
./forgesre doctor
./forgesre status
```

Do **not** run `./install.sh` on a live box.

### 2.2 GLPI version

GLPI 9.5 / 10 / 11 with the built-in REST API (`apirest.php`). The API must be reachable from the Forge VM (same LAN or a URL the Core container can GET). HTTPS with a private CA works if the VM trusts that CA.

---

## 3. GLPI: enable the REST API and create an App-Token

In the GLPI UI (as a Super-Admin):

1. **Setup → General → API**.
2. **Enable REST API** = Yes.
3. **API client** → **Add**:
   - Name: `ForgeSRE`
   - Active: Yes
   - Leave **IPv4 range** empty unless you want to pin the Forge VM IP.
4. Save. Copy the **App-Token** shown once. That value is `GLPI_APP_TOKEN`.

ForgeSRE never uses cookie login, OAuth, or the GLPI UI session.

---

## 4. GLPI: read-only user and User-Token

1. Create (or pick) a user that can **Read** Computer and NetworkEquipment in the entities you want to import. Do not give it Update / Purge / Admin if you can avoid it.
2. Open that user → **Settings** / **Remote access key** (wording varies by GLPI version: *API token*, *Remote access key*, *Application token* on the user).
3. Generate the key and copy it. That value is `GLPI_USER_TOKEN`.

The Core session is `initSession` with `Authorization: user_token <GLPI_USER_TOKEN>` plus the App-Token header. If the user cannot see an itemtype, Discovery stays yellow **No hosts visible**.

---

## 5. ForgeSRE: secrets and update

On the Forge VM, edit `secrets/secrets.env` (mode `600`). The template is [`secrets/secrets.example.env`](../secrets/secrets.example.env) (`# --- GLPI ---`).

```bash
# --- GLPI ---
GLPI_URL=https://glpi.example.local/glpi
GLPI_APP_TOKEN=paste-app-token
GLPI_USER_TOKEN=paste-user-token
```

`GLPI_URL` is the GLPI frontend base (the page you open in a browser). Core appends `/apirest.php`. If your API already lives at `https://host/apirest.php`, you can put that full URL.

Optional YAML override in `config/forgesre.yml` (does not hold the tokens):

```yaml
inventory:
  glpi:
    # enabled: false
    # url: "https://glpi.example.local/glpi"
    auto_sync: false
    timeout_seconds: 4
```

Then:

```bash
./forgesre update
```

Never `./install.sh`. Health cube **GLPI** goes green **Connected** when the session opens and at least one Computer or NetworkEquipment is readable.

---

## 6. ForgeSRE: Discovery → Sync hosts

1. Sign in as an **admin**.
2. Open **Discovery**. The fourth cube is **GLPI**.
3. Click **Sync hosts**.

New items become assets with:

- `source=glpi`
- `glpi_id` like `Computer:12` or `NetworkEquipment:4`
- type **Auto**, empty scrape address
- hostname = GLPI name
- IP from GLPI `IPAddress` when that collection is readable (otherwise empty — fill it on Assets)
- notes = a one-line import snapshot (never rewritten later)
- extras `site` from Location and `customer` from Entity only when those fields are still empty

Existing assets (NetBox, Zabbix, discovery, or manual) are **linked**, not cloned, when the GLPI id / IP / hostname matches. Owner and notes stay yours.

---

## 7. Owner email and NOC fields on imported assets

Imported rows have no owner. Escalation cannot mail anyone until you set **Owner email** (Assets → Edit). Same as NetBox / Zabbix imports.

Filter later with the Assets search box (`glpi`, hostname, IP). The asset page shows **GLPI ID** when `glpi_id` is set.

---

## 8. Optional: automatic import

Default is **manual** (`inventory.glpi.auto_sync: false`). To import with the 6 h discovery loop:

```yaml
inventory:
  glpi:
    auto_sync: true
```

Then `./forgesre update`. The Discovery **Sync hosts** button still works.

---

## 9. Troubleshooting

| Symptom | What it usually is |
|---|---|
| Cube **Not configured** / Sync hosts grey | Empty `GLPI_URL`, `GLPI_APP_TOKEN` or `GLPI_USER_TOKEN`. Edit secrets, `./forgesre update`. |
| Cube **Unreachable** (yellow) | Forge VM cannot GET `GLPI_URL/apirest.php`. Firewall, TLS, or wrong base URL. |
| Cube **API error** | Bad App-Token / User-Token, API disabled, or IPv4 range on the API client excludes the Forge VM. |
| **No hosts visible** | Tokens work; the user has no Read on Computer / NetworkEquipment in that entity. |
| Sync created 0, linked 0 | Same as no hosts, or every item already linked. |
| Asset has no IP | GLPI did not expose `IPAddress` for that item. Type the IP on Assets. ForgeSRE does not invent one. |
| Health stays yellow after a fix | Status is cached up to 2 minutes. **Run doctor** or wait, then refresh Discovery. |

ForgeSRE **never** writes to GLPI. If a name is wrong, fix it in GLPI and Sync again only fills empty ForgeSRE fields.

---

## 10. End-to-end operator checklist

1. GLPI API enabled, client `ForgeSRE` created, App-Token copied.
2. Read-only user can see Computer and NetworkEquipment; User-Token copied.
3. `GLPI_URL`, `GLPI_APP_TOKEN`, `GLPI_USER_TOKEN` in `secrets/secrets.env`.
4. `./forgesre update` (not `./install.sh`).
5. **Discovery → GLPI** cube is **Connected** (or **No hosts visible** if the entity is empty).
6. **Sync hosts** — new assets appear on Assets with source `glpi`.
7. Set **Owner email** on those assets.
8. Optional: `inventory.glpi.auto_sync: true` for the 6 h loop.

---

## 11. After git pull

```bash
git pull origin main && ./forgesre update
```

Hard-refresh Discovery so `app.css` is not served from cache. Secrets are not regenerated.
