# Session handoff — 9 September 2026

This file is a **session handoff for the next coding agent or contributor**. It is not an operator manual. Operators start at [install and config](install-config.md) and the [operator handbook](operator-handbook.md).

Product on `main` at the end of this session: **V0.7**. Repository: https://github.com/nsimic2022/forgesre.

1. [Who and when](#1-who-and-when)
2. [Checked twice (pytest)](#2-checked-twice-pytest)
3. [Done today / on this branch](#3-done-today--on-this-branch)
4. [What N should do on the VM](#4-what-n-should-do-on-the-vm)
5. [Product facts not to redo](#5-product-facts-not-to-redo)
6. [How to continue next session](#6-how-to-continue-next-session)
7. [Out of scope](#7-out-of-scope)
8. [Known leftovers](#8-known-leftovers)

---

## 1. Who and when

**Tuesday 8 September 2026.** Operator N asked for small GUI fixes on V0.7 (dashboard bar, clickable tiles, Assets / Discovery / Incidents / History / Playbooks / Admin users). English UI. No React. Do not revert ⓘ tooltips (`410fbb5`) or `/ops` scheduled-report Edit/Clone/Remove (`edba058`).

Code and docs stay English. Replies to N are Serbian.

**Never** re-run `./install.sh` on a live box. That regenerates passwords in `secrets/secrets.env` and will wipe the install admin the operator already uses. Never print tokens.

---

## 2. Checked twice (pytest)

From this branch, after `pip install -r requirements-dev.txt`:

```bash
PYTHONPATH=backend:agents python3 -m pytest tests
PYTHONPATH=backend:agents python3 -m pytest tests
```

Pytest count after the double run on `cursor/nav-clock-resources-05f8` (rebased onto `origin/main` `471fd97` GUI small-fixes): **428 passed** (twice on the previous rebase). Was **417** after report-job actions, **425** for GUI small-fixes, **420** for clock/resources before that GUI tip. Report-job row actions CSS kept. ⓘ tooltips kept. Do not revert `471fd97`.

If pytest fails next session: fix on a `cursor/<name>-05f8` branch, re-run **twice**, then `git merge --no-ff` to `main`. Branch pattern `cursor/<name>-05f8`. `create_pr` often **403** — merge `--no-ff` plus `git push origin main` still lands the change.

---

## 3. Done today / on this branch

### Discovery multi-CIDR autodetection (not /24-only)

- **Scan now** enumerates **all connected IPv4 networks** on the appliance (Core `network_mode: host`) with each interface’s **real prefixlen** — never hardcodes `/24`.
- Skips loopback, link-local, multicast, `0.0.0.0/0`, and Docker bridges (`docker0`, `br-*`, `veth*`) by default.
- UI prefills saved `discovery.cidrs` or detected CIDRs; operator may edit; Scan now **saves** to live YAML and probes.
- Limits stay **256/CIDR**, **1024** total; huge prefixes truncate with a **UI warning**. Ports: TCP **22, 80, 443, 9100, 9182** + SNMP **UDP/161**. Not nmap. Approve queue unchanged.
- Candidate table: **Open ports**, **node_exporter**, **SNMP**. Flags persisted on candidates (migrate).
- Layout: **Scan now** and **NetBox sync** half-width side-by-side (`.discovery-actions`). CSS/JS cache `app.css?v=disc-1` / `app.js?v=disc-1`.

Do not revert ⓘ tooltips, nav clock/resources, or `/ops` report-job row actions.

## 4. What N should do on the VM

Do **not** run `./install.sh`. Hard-refresh the UI after update (CSS/JS cache `app.css?v=nav-1` and `app.js?v=nav-1`: clock, appliance glance, logout spacing).

Hard-refresh after update (`app.css?v=disc-1`).

```bash
git pull origin main && ./forgesre update
```

Open **Discovery**, review autodetected connected CIDRs (edit if needed), then **Scan now**.

**Enabled** = the job fires at **Next**. **Disable** keeps the row; the scheduler skips it. That is not Remove. Edit / Clone / Remove sit on each row; Cancel sits next to Save on the form.

NetBox: keep the **full v2 token** (shown once at create, not the 12-character key) in `NETBOX_API_TOKEN`. Recreate **core** only if secrets changed. Never print the token.

```bash
docker compose logs netbox | grep forgesre
```

Expect **`skipping v1 upsert`** (v2 already in secrets) or **`v1 token ready`** (legacy 40-char). Not **`could not upsert`** for a valid v2 secret.

**200** on `GET /api/dcim/devices/?limit=1` = Core can sync (Discovery yellow if empty, green if ≥1 device). **403** with a v2 secret = not the full `nbt_…` string, or core not recreated. Recreate **core**. Never print the token.

Lab without image pull: `./forgesre update --offline`.

Learn the box in the order in [`docs/README.md`](README.md).

---

## 5. Product facts not to redo

These already work on `main`. Do not “fix” them unless N asks.

- `./forgesre test` = appliance health report → `data/reports/`. `./forgesre verify` = live inventory communication. `./forgesre ping` = ICMP + exporter. `./forgesre doctor` = System Health lights. Three different commands (`test` / `verify` / `doctor`).
- Theme toggle cycles **light → dark → system**. Left nav stays dark.
- Dashboard demos are **one** top-right button + a closeable panel. Demo rows stay **labeled DEMO**. Demo inject is **admin**.
- Incident ids look like `INC-0134_16.08.2026_09:13`.
- RCA is Python under `agents/rca/`. The LLM only rewrites prose. Builtin ForgeRCA always runs first. LLM payload is compact (5000 chars); stored investigation is full.
- Core is an SMTP **client** only. The UI has no IMAP inbox. One mail outbox: `/ops#mail`.
- pytest is a laptop/dev dependency. The Core image must not install it.
- Real Windows scrape is **windows_exporter :9182**, not the lab demo host.
- Prometheus Health Open is **Targets** (`:9090/targets?search=`), not Prometheus process `/metrics`. Core `/metrics` stays.
- Host CLI must not require sqlalchemy/PyYAML. Do not `pip install sqlalchemy` on the Ubuntu host.
- `snmp-exporter` is a **default** compose service. No SNMP targets → doctor **paused (no SNMP targets)** (yellow), not DOWN. Do not un-pause by adding dummy devices.
- Bundled **NetBox** is a **default** compose service (`:8001`). Do not put it behind a profile. `--netbox-url` remains an external override. Image pin is `netboxcommunity/netbox:v4.6.9-5.0.2`. Do not churn NetBox Hub/GHCR tags unless N asks. Prefer a NetBox UI **v2** token (full `nbt_…`) in `NETBOX_API_TOKEN`; Core sends `Authorization: Bearer`. `scripts/netbox-upsert-token.py` **skips** v1 create for that secret. A 40-character **v1 plaintext** value is still upserted as fallback (`write_enabled=False`) on the **superuser**. Success log: **`skipping v1 upsert`** or **`v1 token ready`**. **`could not upsert`** means a v1 secret never landed in the NetBox DB (UI still starts; Discovery 403 is honest). Never touch `User.is_staff` (removed in NetBox 4.5). Core is **not** a NetBox UI login. Do not drop database `forgesre` to “fix” NetBox. Health NetBox tile: UI up + 403 = **warn**, not paused. HTTP 200 with v2 is yellow (0 devices) / green (≥1).
- NetBox UI **API token peppers not defined**: v4.5+ needs `API_TOKEN_PEPPERS`. Official image reads `API_TOKEN_PEPPER_1`. ForgeSRE generates `NETBOX_API_TOKEN_PEPPER` once in `secrets/secrets.env` (and `.env`) via `ensure-netbox-secrets.sh`. Extra config: `config/netbox/forgesre.py` → `/etc/netbox/config/forgesre.py`. Do not re-run `./install.sh`.
- Dashboard **HOST DOWN** banner (open exporter/SNMP-down incidents). Do not redo it. That banner is **not** the Prometheus doctor journal.
- Backup on the host dumps Postgres via `docker compose exec postgres` with the same docker rights as `./forgesre update`.
- One restore unit = one `.tar.gz` inside `backup_<stamp>/`.
- Host `./forgesre verify` does not import sqlalchemy (`demo_ids.py`).
- Memory bundled alerts exist: `NodeMemoryHigh` / `WindowsMemoryHigh` at **90%**, playrules `node-memory` / `windows-memory`. Grafana is not the alarm path. Grafana doctor down is **yellow**, not a Prom FAIL.
- Add asset: operator types **Asset ID** and **Hostname** separately. Id is immutable after create.
- Doctor labels: **Core API** vs **Core (container)**. Core `/api/v1/health` stays a liveness dummy (always-ok). Prom readiness is `/-/ready` on `:9090`.
- The lab SMTP catcher is gone. Do not add one.
- Verify hops: ICMP, PORT, FAMILY, PROM, TARGET, SERIES, AM, CORE, RCA, LLM. Reachability: ping **green** ICMP ok; **yellow** ICMP fail but exporter/SNMP ok; **red** both fail.
- Linux metrics = node_exporter **:9100**. Windows = windows_exporter **:9182**. Network = snmp_exporter :9116.
- Bundled LLM pin = **Qwen2.5-14B-Instruct Q4_K_M** via `./forgesre fetch-llm`. Do **not** restore the GGUF catalog / Health picker.
- `docs/architecture.md` is a **proposal**, not the appliance runtime. **architecture proposal** / **not the V0.7 appliance runtime**.
- GUI ICMP is from the Core container (`iputils-ping` in the Dockerfile). Host `./forgesre ping` stays on the VM.
- Jobs: **one worker thread** in Core. There is no Celery.
- GUI list tables are **10 rows per page** (pagination already on `main`). Do not revert it.
- `/ops#reports` scheduled jobs: **Edit / Clone / Remove / Enable** on the row; **Cancel** next to Save. **Enabled** = fire at Next; off = stored, skipped. Postgres `scheduled_reports`. No Celery.
- Discovery **Sync NetBox**: primary (orange) admin POST when the UI answers (`/login/` or `/api/status/`). Devices API 403 is a warning, not `disabled`. First-boot (UI down) stays disabled with one sentence. Engineer/analyst see disabled + **Admin only.** Viewers cannot open `/discovery`. Never write back to NetBox. Bundled footer must not say “external instance”. Prefer UI v2 in `NETBOX_API_TOKEN`; launch skips v1 upsert for `nbt_…`; 40-char v1 is fallback. `update` `--force-recreate`s `netbox`. The chip next to Sync is status only (Not connected / API 403 / No devices / Connected) — grey/yellow/green CSS, not a second button. Sentence *No devices yet; add in NetBox UI `:8001` or use Assets/Discovery.* stays on yellow. 403 with a v1 token present says rejected (recreate netbox+core); 403 with v2 says full `nbt_…` then recreate **core** — not “token missing”.

Also: `./forgesre ping` and `./forgesre verify` stay distinct from `./forgesre test` / doctor. See [`docs/llm.md`](llm.md).

---

## 6. How to continue next session

1. `git pull origin main`.
2. Read **this file**, then the [docs index](README.md), [`docs/llm.md`](llm.md) and [`docs/cli.md`](cli.md).
3. On the VM: `git pull origin main && ./forgesre update`. Keep the full v2 token in `NETBOX_API_TOKEN`. `docker compose logs netbox | grep forgesre` should say **skipping v1 upsert** (v2) or **v1 token ready** (fallback), not could not upsert for a valid v2 secret. Never `./install.sh`. Never print tokens.
4. `pip install -r requirements-dev.txt` if needed, then `PYTHONPATH=backend:agents python3 -m pytest tests` **twice**, then merge to `main`. Branch pattern `cursor/<name>-05f8`.
5. Replies to N are in **Serbian**. OSS docs and code stay in **English**.
6. `ManagePullRequest` `create_pr` often 403. `git merge --no-ff` plus `git push origin main` still lands the change.

---

## 7. Out of scope

Do not start these unless N asks:

- Go / Kubernetes rewrite in [`docs/architecture.md`](architecture.md).
- Zabbix templates, or ticketing as a second Ticket object.
- IMAP inbox in the UI.
- React, Tailwind, Bootstrap, PatternFly npm.
- Fake a live Windows scrape or SNMP walk in the demo panel.
- Explode backup tars into many small files at `data/backups/` root.
- Grafana deep-link on the asset page (N said later).
- Rewriting all of Prometheus `alerts.yml` per asset.
- Load, inodes, blackbox, mysql/redis exporters in compose.
- 50 collectors dropdown on Add asset.
- Celery / Redis job queue / `SKIP LOCKED`.
- A second log stack (host Alloy for every asset).
- NetBox Docker Hub / GHCR tag churn (separate).
- Rewriting discovery as nmap.
- Making Ollama the product default (N will decide later).
- Re-pinning bundled `fetch-llm` to Qwen2.5-1.5B or another GGUF.
- Restoring the llama.cpp GGUF catalog / Health model switcher.
- Raising `timeout_seconds` in `config/forgesre.example.yml` back to 600.
- Reverting GUI list pagination.

---

## 8. Known leftovers

- Many remote `origin/cursor/*-05f8` branches still exist and are **already merged to `main`**.
- Scheduled `/ops` reports are still plain text. Row actions (Edit / Clone / Remove / Enable) are done; do not add an IMAP inbox or Celery.
- ⓘ GUI help tooltips are on `main`. This branch rebased; `/ops` keeps the include.
- Old backups already on the VM as `data/backups/forgesre-*.tar.gz` are still valid; new runs write folders.
- Grafana deep-link from an asset is still later.
- Prometheus global rules may still fire for a host whose ForgeSRE alarm is disabled or raised; ForgeSRE will not open the incident when the webhook carries the value.
- Alloy still only ships appliance Core logs as `forge-demo-01`. Real hosts have no Loki until that changes. Limitation: **no host logs shipped**.
- LLM rewrite can still occupy the single job loop for up to `timeout_seconds` (default 90 in example.yml; N’s live yml may be 300) after reports in that pass have already run.
- `config/forgesre.yml` on a live VM is gitignored; a 300s or 600s timeout already written there is not overwritten by `update`.
- Loki/Alloy still FAIL doctor when those containers are dark (not Grafana). Only Grafana was moved to warn.
