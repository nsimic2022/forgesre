# ForgeSRE operator CLI

This page is the **command list**. Why and when: [operator handbook](operator-handbook.md). Learning order: [docs index](README.md).

All commands run **on the VM**, from the clone directory (`~/forgesre`). ForgeSRE does not speak SSH of its own — you SSH to Ubuntu, then use this CLI on localhost.

```bash
ssh you@forgesre-vm
cd ~/forgesre
./forgesre help
```

`./f` is the same binary. `./forgesre` with no arguments opens a prompt (`forgesre>`). Type `journal`, `incidents`, `doctor` — not `./forgesre` again. Leave with `quit`, `exit`, or Ctrl-D (`./forgesre help quit`). TAB completes command names, Compose services (`logs sn<TAB>`), incident ids, and asset numbers/ids/hostnames (`verify 1<TAB>`). GUI tables are **10 rows per page** by default (tabs plus Previous / Next, **Rows** 10 / 20 / 50 / 100 bottom-right, `?per_page=`); the CLI lists are unchanged.

Two logins:

1. **Linux SSH** — OS account on the VM.
2. **ForgeSRE user** — Administration → users. `./forgesre login` stores `data/cli.session`. Without it, the CLI uses the install admin from `secrets/secrets.env` when that file is readable.

---

## Everyday commands

```bash
./forgesre help
./forgesre help quit
./forgesre help test
./forgesre help snmp
./forgesre                 # prompt (forgesre>); leave with quit
./forgesre doctor          # short lights (Grafana yellow ≠ Prom down)
./forgesre ping            # ICMP + exporter /metrics (alias: probe)
./forgesre ping win10-gp
./forgesre verify          # live chain: exporter → prometheus → alertmanager → core (not test)
./forgesre verify 12                 # asset number #
./forgesre verify win10-gp           # Asset ID
./forgesre verify DESKTOP-CG81N3J    # hostname
./forgesre verify 10.10.10.60        # IP
./forgesre test            # detailed report → data/reports/
./forgesre status          # docker compose ps
./forgesre logs core
./forgesre config
./forgesre assets
./forgesre snmp
./forgesre sd
./forgesre incidents
./forgesre history --days 90
./forgesre jobs
./forgesre journal
./forgesre journal snmp
./forgesre demo
./forgesre demo-reset
./forgesre secrets-check
./forgesre render-monitoring
./forgesre snmp-auths           # per-asset Custom community / v3 → snmp.yml, reload snmp_exporter
./forgesre backup
./forgesre backup --no-secrets
./forgesre backup --include-models
./forgesre restore                 # numbered backup_* picker (newest first); still needs --yes
./forgesre restore data/backups/backup_YYYYMMDDTHHMMSSZ --yes
./forgesre import backup           # same picker as restore
./forgesre remove backup           # numbered picker; delete that folder only with --yes
./forgesre mailbox         # optional; does not rewrite Gmail/Outlook SMTP
./forgesre fetch-llm
./forgesre update           # includes bundled NetBox :8001 (wait on first boot)
./forgesre update --offline # lab: no image pull; skip Core --build if sources unchanged
./forgesre restart          # restart existing containers only; no git pull, install.sh, or secrets/.env writes
./forgesre version
./forgesre login
./forgesre whoami
./forgesre logout
```

`./forgesre update` starts default Compose services including **snmp-exporter** (`127.0.0.1:9116`) and bundled **NetBox** (`http://<VM-IP>:8001`). First NetBox boot runs Django migrations and can take several minutes; `./forgesre doctor` stays **yellow** until `http://127.0.0.1:8001/login/` answers. Wait — that is not a fake green. `--offline` skips `compose pull` (lab / no egress); Core `--build` is skipped when `backend/Dockerfile`, `backend/`, `agents/`, and `frontend/` are unchanged.

Root wrappers still work: `./install.sh`, `./doctor.sh`, `./test.sh`, `./backup.sh`, `./restore.sh`, `./update.sh`.

### All commands

Same list as `./forgesre help`, TAB completion (`scripts/forgesre-completion.bash`), and **Administration → ForgeSRE CLI**. `./forgesre help <command>` prints details and examples for any of them.

| Command | What it does |
|---|---|
| `update` | Backup, render monitoring, refresh images, compose up, `snmp-auths`, doctor. Use after `git pull`. `--offline` skips image pull. |
| `restart` | Restart containers that already exist (Postgres first, Core last). No git pull, install, render, or `.env` / `secrets/` writes. |
| `render-monitoring` | Rewrite `data/generated/{prometheus,alertmanager,snmp,alerts}.yml` from templates; reload snmp_exporter and Prometheus. |
| `snmp-auths` | Write per-asset SNMP auths (Custom community / v3) into `snmp.yml`; reload snmp_exporter. |
| `status` | `docker compose ps` |
| `logs [service]` | Container logs (last 100 lines). TAB completes service names. |
| `config` | Print `config/forgesre.yml` |
| `version` | `FORGESRE_VERSION` from `.env` |
| `secrets-check` | Refuse-to-start defaults: `SECRET_KEY` / webhook token |
| `install` | Guided first-time install — **new VM only** |
| `doctor` | Short health lights (same as `/health-ui`) |
| `test` | Full appliance report → `data/reports/` |
| `verify [asset]` | Live chain: exporter → prometheus → alertmanager → core |
| `ping [asset]` / `probe` | ICMP + exporter `/metrics` from this host |
| `assets` / `inventory` | Inventory with `#` |
| `snmp` | snmp_exporter health + SNMP HTTP SD targets |
| `sd` | Prometheus HTTP SD (Linux / Windows) and SNMP HTTP SD |
| `incidents [INC-…]` | Short colored board, or one incident |
| `history` | 90-day CLI lookback (`--days`, `--status`, `--asset`, `INC-…`). The GUI list is Incidents; `/history` redirects there. |
| `jobs` | Background job queue (RCA, discovery scans) |
| `journal [module]` | Internal console reports (JSON) |
| `demo` / `demo-rca` / `demo-reset` | Demo HighCPU path / filesystem RCA demo / lower the demo gauges |
| `backup` | One folder per run under `data/backups` (`--no-secrets`, `--include-models`) |
| `restore` / `import` | Numbered backup picker; applies only with `--yes` after stopping Core |
| `remove backup` | Delete one backup folder (`--yes` or typed yes) |
| `fetch-llm` | Download the GGUF into `data/models/model.gguf` and start llama.cpp |
| `mailbox` | Optional Postfix/Dovecot + Roundcube (Core SMTP unchanged unless `--bind-core`) |
| `tls` | How to put optional HTTPS (Caddy) in front of Core |
| `login` / `logout` / `whoami` | ForgeSRE user for the CLI (`data/cli.session`) |
| `shell` | The `forgesre>` prompt (same as no arguments) |
| `completion` | Print a `source …` line so TAB works in your login bash |
| `help [command]` | Overview, or details for one command |
| `quit` / `exit` | Leave the `forgesre>` prompt (Ctrl-D also works) |

### Update vs restart

| | `./forgesre update` | `./forgesre restart` |
|---|---|---|
| When | After `git pull origin main` (new code, templates, images) | A container is wedged; you want fresh processes on the same config |
| Backup | Yes (continues if it fails) | No |
| Render Prometheus / Alertmanager / snmp.yml | Yes, plus `snmp-auths` | No |
| Image pull / Core rebuild | Pull unless `--offline`; Core `--build` only when sources changed | No |
| Containers | `docker compose up -d` (creates missing ones) | `docker compose restart` of existing ones only; profile-off services skipped |
| `.env` / `secrets/` | Only fills missing keys | Never touched |

Neither runs `./install.sh`. If `restart` says Postgres or Core has no container yet, run `update`.

### Leave the prompt

```bash
./forgesre                 # opens forgesre>
journal
incidents
quit                       # or: exit    or Ctrl-D
./forgesre help quit
```

`quit` is a prompt command. From host bash you are already out; `./forgesre quit` only prints the same help.

---

`./forgesre config` shows the live YAML path. Discovery **Confirm & scan** on `/discovery` writes `discovery.cidrs` there (suggested management `/24` from this VM’s primary IPv4; empty cidrs = no scan). Do not re-run `./install.sh` just to change CIDRs.

---

**Zabbix has no CLI command.** There is no `./forgesre zabbix`. Connect it with `ZABBIX_URL`, `ZABBIX_API_TOKEN`, `ZABBIX_WEBHOOK_TOKEN` in `secrets/secrets.env` (not `.env`, not Administration), then `./forgesre update`; import hosts with **Discovery → Sync hosts** in the UI. Status: `./forgesre doctor` (component `zabbix`, yellow = optional source / timeout) and the **Zabbix** cube on System Health. Console: `./forgesre journal zabbix`. Incidents: `./forgesre incidents`. `./forgesre verify` is the Prometheus chain, not the Zabbix webhook. `./forgesre snmp-auths` is for ForgeSRE's own snmp_exporter and is not needed for Zabbix. Full cookbook (Zabbix server clicks + ForgeSRE + checklist): [zabbix.md](zabbix.md).

---

## Ping vs scrape

ICMP ping from the appliance only proves **L3** (the host answers ping). ForgeSRE **sees** a host when Prometheus scrapes exporter `/metrics`. `./forgesre ping` (alias `./forgesre probe`) checks both **from the Ubuntu host**, using inventory already in ForgeSRE — no extra flags for the common case. Do not `pip install sqlalchemy` on the host.

GUI Verify / asset Ping badges run **inside the Core container**. The Core image installs `iputils-ping` so that ICMP is the same idea as host `./forgesre ping` (one ICMP echo). If ping were missing in Core, the GUI would show `ping not installed (iputils-ping)` instead of a real miss.

```bash
./forgesre ping
./forgesre ping 12
./forgesre ping win10-gp
./forgesre ping DESKTOP-CG81N3J
./forgesre ping 10.10.10.60
./forgesre probe              # same command
./forgesre help ping
```

| ICMP | METRICS | Meaning |
|---|---|---|
| PASS | PASS | Host is up and the exporter answers. Prometheus can scrape it (wait ~30s, then `./forgesre sd`). |
| PASS | FAIL | Host is on the network; ForgeSRE still cannot see it. Windows: `windows_exporter` not running, firewall **TCP 9182**, or scrape port is Linux **:9100**. Linux: `node_exporter` / **TCP 9100**. |
| FAIL | FAIL | Wrong IP, host down, or ICMP and the exporter port both blocked. |
| PASS | SKIP | Network device — HTTP metrics do not apply. Use `./forgesre snmp` (UDP/161). |

Linux default scrape is `:9100`. Windows Server default is `:9182`. Configured `scrape_address` wins. An ad-hoc IP (no inventory type) **and** an inventory row with empty scrape + Unknown/Auto type probe **both** ports and classify `windows_` vs `node_` the same way Assets/Discovery detect does (both → prefer Windows `:9182` unless a saved type exists). Seeded `forge-demo-*` rows and discovery seed `10.20.30.41` are skipped unless you pass `--demo` or the id.

---

## Verify (live communication)

`./forgesre test` is appliance health (files, Compose, login, APIs) after `update`. **`./forgesre verify` is a different command**: live communication for inventory already in ForgeSRE. **`./forgesre doctor`** is System Health lights. Grafana down is yellow (graphs only) — not a Prometheus FAIL and not a Journal error. A real Prom/Alertmanager failure names `:9090` / `:9093`. Those three commands are not the same. Verify runs on the **host** CLI (Ubuntu Python has no sqlalchemy — do not pip-install it). GUI Verify still runs inside Core (ICMP via `iputils-ping` in the Core image).

```bash
./forgesre verify
./forgesre verify 12
./forgesre verify win10-gp
./forgesre verify DESKTOP-CG81N3J
./forgesre verify 10.10.10.60
./forgesre verify --demo
./forgesre help verify
```

Path: inventory row → ICMP / exporter (`:9100` `node_` or `:9182` `windows_`) or SNMP UDP/161 → Prometheus `up` + scrape target health + family series → Alertmanager reachable → last Core incident/webhook (SKIP if none). ForgeAI/LLM is listed only when enabled; verify does **not** call the LLM. Chain: `exporter → prometheus → alertmanager → core`.

If the row has an IP but **no** `scrape_address` and type is Unknown / Auto / empty, verify still probes **`:9100` and `:9182`** on that IP (same live GET as Add Auto). `node_` → Linux Server `:9100`, `windows_` → Windows Server `:9182`. GUI Verify and `./forgesre verify` **save** type + scrape so Prometheus HTTP SD can pick the host up (wait one refresh, then `./forgesre sd`). ICMP ping alone is not a scrape.

Detect/verify GET `/metrics` with a few-second timeout, read until `node_` / `windows_` is visible (those families often sit after `go_*`), then **drain** the rest of the body. Closing after 2–4 KiB was `error encoding and sending metric family: write: broken pipe` on node_exporter. Prometheus inventory scrape is a different client (`scrape_timeout` default 10s); a huge `/metrics` vs a short scrape timeout is an ops issue, not a new exporter.

Classes are universal, not SKUs: Linux, Windows, Network SNMP, Unknown. Unknown or a missing exporter / no Prom target is **SKIP or FAIL with an honest reason** — never a fake green host. Seeded `forge-demo-*` rows and the discovery Approve seed `10.20.30.41` (`disc-10-20-30-41`) are **lab** (label DEMO), are not in HTTP SD, and are not proof of a real scrape. Verify does **not** call the LLM even when ForgeAI is enabled.

`verify` accepts **all of**: asset number `#`, Asset ID, hostname, and IP. Same keys work for `./forgesre ping`. TAB completes numbers and ids (hostnames too). One key dumps what ForgeSRE already knows (inventory) plus the live checks. Same action: Assets → **Verify** (analyst / engineer / admin). Viewers are read-only.

`./forgesre jobs` is the Postgres job table. There is **no Celery**. One worker thread in Core runs scheduled reports then pending jobs (`investigate` and `discovery_scan`). Discovery **Confirm & scan** / **Scan now** enqueue `discovery_scan` (`status=pending`); the worker runs `run_scan` in try/except so a probe crash becomes `job.error` + Journal, not a Core 500. An LLM rewrite can occupy that thread up to `ai.llm.timeout_seconds`. Scheduled `/ops#reports` jobs are a different table (`scheduled_reports`): **Edit / Clone / Remove** on the row, **Cancel** next to Save on the form, **Enabled** = fire at Next (off = stored, skipped). Same SMTP send path as Compose.

---

## SNMP

```bash
./forgesre snmp             # exporter :9116 health + SNMP HTTP SD targets
./forgesre snmp-auths       # after saving a Custom community / v3 asset
./forgesre logs snmp-exporter
./forgesre help snmp
./forgesre help snmp-auths
```

Network devices (type Network device / Switch / Router / Firewall / Storage / QNAP/NAS / Printer with an IP, or any asset with an SNMP port) are walked by the bundled snmp_exporter over **UDP/161** (or the asset's SNMP port).

- **v1 / v2c** — Community *Default* = `SNMP_COMMUNITY` in `secrets/secrets.env` (snmp.yml auth `public_v2`, or `public_v1` for v1). *Custom* = a community for this host only.
- **v3** — USM user, security level, auth / privacy protocol and passwords.
- Custom and v3 need their own auth `forgesre_<asset id>` in `data/generated/snmp.yml`. `./forgesre snmp-auths` fetches them from Core (`GET /api/v1/sd/snmp-auths`, Bearer webhook token), rewrites only the `forgesre-asset-auths` block (mode 600), and reloads snmp_exporter. `update` and `render-monitoring` run it too. Until then SD keeps the default `public_v2` auth and the asset page shows *pending*. Secrets are never printed.
- **VLAN** is an inventory field (search / filter). It does not change SNMP polling and ForgeSRE never writes switch config.
- `snmp.yml` carries a valid snmp_exporter v0.26 `if_mib` module (ifDescr / ifName / ifAlias as per-metric lookups), so the exporter **stays up** after `update`. `render-monitoring` always reloads it; if it was not answering yet, `docker compose restart snmp-exporter`. With the exporter up, **SnmpDeviceUnreachable** (`up{job="forgesre-snmp"} == 0` for 2m) means the **device** did not answer — UDP/161 firewall, device ACL, community or v3 user — not a broken exporter.

Testing the alert end to end needs a real SNMP agent (switch, or `snmpd` on a lab host). The seeded demo switch is not a live walk.

---

## Advanced CLI

Use on a live box. **Never** `./install.sh` again — that regenerates passwords.

### Git pull (existing VM)

```bash
git checkout main
git pull origin main
./forgesre update
./forgesre test
./forgesre snmp
```

`./forgesre update` = doctor (warn ok) → backup → render-monitoring → compose pull (unless `--offline`) → `docker compose up -d` (includes **snmp-exporter** on `127.0.0.1:9116` and bundled **NetBox** on `:8001`; Core `--build` only when Dockerfile/backend/agents/frontend changed) → `./forgesre snmp-auths` once Core answers → wait for NetBox `/login/` (first boot can take several minutes; doctor stays yellow) → doctor.

Backup on the host does **not** use sqlalchemy (that package is only in the Core image). The CLI dumps Postgres with `docker compose exec postgres` using the **same docker rights as update** (`docker info`, otherwise `sudo docker compose`). Administration Backup still runs inside Core. Each run is `data/backups/backup_YYYYMMDDTHHMMSSZ/forgesre.tar.gz` (plus `MANIFEST.txt`); import that one tar, do not unpack it. `./forgesre backup` still exists; `./forgesre update` also runs backup as a safety net. If backup fails, update prints a clear error and **continues** so the stack still comes up. A `docker.sock` permission error is not “postgres is down”. Do not `pip install sqlalchemy` on the host. Do not `./install.sh`.

### Rebuild Core after Python changes

```bash
docker compose build core
docker compose up -d core
docker compose ps core
```

### Logs (Core, RCA, LLM)

```bash
docker compose logs --tail=100 core
docker compose logs --tail=100 core | grep -iE "llm|rca|error|exception"
docker compose logs --tail=50 core | grep "/ai"
docker compose logs -f core
docker compose logs --tail=200 llm
./forgesre logs core
./forgesre logs llm
```

### LLM container

Full implementation guide (including health inspect and `/v1/chat/completions`): [`llm.md`](llm.md) §8.

```bash
./forgesre fetch-llm
# lab 8 GB RAM: wget Qwen3-4B into data/models/model.gguf, then:
./forgesre fetch-llm --offline
docker compose --profile ai up -d llm
docker compose ps llm
docker compose ps -q llm | xargs -r docker inspect --format='{{json .State.Health}}'
curl -sS http://127.0.0.1:8088/v1/models
curl -sS http://127.0.0.1:8088/health
docker compose logs --tail=100 llm
docker compose logs --tail=100 core | grep -iE "llm|openai|model|error|exception"
docker compose logs -f llm
./forgesre logs llm
./forgesre doctor
./forgesre test
```

Do not `docker compose down` to “fix” LLM — that stops the whole appliance.

### Inside the Core container

```bash
docker compose exec -T core pwd
docker compose exec -T core ls
docker compose exec -T core python -c "import sys; print('\n'.join(sys.path))"
```

### VMware guest tools

```bash
sudo apt install -y open-vm-tools
sudo systemctl enable --now open-vm-tools
systemctl status vmtoolsd
```

`config/forgesre.yml` is local to the VM (gitignored). Discovery **Confirm & scan** writes `discovery.cidrs` there (suggested primary IPv4 `/24`; empty = no scan). Change other keys, then recreate Core. Do not commit the live file. The committed template is `config/forgesre.example.yml`.

---

## API (session cookie)

After `./forgesre login` or a UI login cookie:

| Method | Path | Who |
|---|---|---|
| POST | `/api/v1/users` | admin (create) |
| POST | `/api/v1/users/{id}` | admin (edit) |
| POST | `/api/v1/users/{id}/delete` | admin |
| GET | `/api/v1/assets` | viewer+ |
| POST | `/api/v1/assets` | analyst+ |
| GET | `/api/v1/assets/{id}/verify` | analyst+ (live path; same as `./forgesre verify <id>`) |
| GET | `/api/v1/assets/{id}/metrics` | viewer+ (class tiles: CPU/mem/disk/up from Prometheus) |
| GET | `/api/v1/verify` | analyst+ (all real assets; `?include_demo=1` labels DEMO) |
| POST | `/api/v1/assets/{id}` | analyst+ (edit) |
| POST | `/api/v1/assets/{id}/clone` | analyst+ |
| POST | `/api/v1/assets/{id}/delete` | analyst+ |
| GET | `/api/v1/history` | viewer+ |
| GET | `/api/v1/health` | none (Core liveness; `./forgesre doctor` Core-up probe) |
| GET | `/api/v1/system/doctor` | login or Bearer webhook token |
| GET | `/api/v1/sd/prometheus` | Bearer webhook token |
| GET | `/api/v1/sd/snmp` | Bearer webhook token |
| GET | `/api/v1/sd/snmp-auths` | Bearer webhook token (per-asset auths for `./forgesre snmp-auths`) |
| POST | `/api/v1/webhooks/alertmanager` | Bearer webhook token |

Install and file layout: [`install-config.md`](install-config.md). Verification report: [`verify.md`](verify.md). Local LLM: [`llm.md`](llm.md).
