# Session handoff — 9 September 2026

This file is a **session handoff for the next coding agent or contributor**. It is not an operator manual. Operators start at [install and config](install-config.md) and the [operator handbook](operator-handbook.md).

Product on `main`: **V0.7**. Repository: https://github.com/nsimic2022/forgesre.

1. [Who and when](#1-who-and-when)
2. [Checked twice (pytest)](#2-checked-twice-pytest)
3. [Done today / on this branch](#3-done-today--on-this-branch)
4. [What N should do on the VM](#4-what-n-should-do-on-the-vm)
5. [Product facts not to redo](#5-product-facts-not-to-redo)
6. [How to continue next session](#6-how-to-continue-next-session)

---

## 1. Who and when

**Wednesday 9 September 2026.** Discovery **Save & scan** / **Scan now** 500’d (read-only YAML mount + in-request probe). Scan is a Postgres job; YAML mount is rw; autodetect never 500s. Branch: `cursor/discovery-scan-500-05f8`. Prefer `git merge --no-ff` when PR create is 403.

**Never** re-run `./install.sh` on a live box. That regenerates passwords in `secrets/secrets.env`. Never print tokens. Never commit real secrets.

Replies to N are in **Serbian**. OSS docs and code stay in **English**.

---

## 2. Checked twice (pytest)

```bash
PYTHONPATH=backend:agents python3 -m pytest
PYTHONPATH=backend:agents python3 -m pytest
```

Record pass counts after both runs. `create_pr` / `ManagePullRequest` often **403** — `git merge --no-ff` plus `git push origin main` still lands the change.

---

## 3. Done today / on this branch

### Discovery scan is a Postgres job (not inline, not Celery)

- `POST /discovery/scan` (**Save & scan** and **Scan now**) and `POST /api/v1/discovery/scan` **enqueue** `jobs.kind=discovery_scan` with `status=pending`. They do **not** call `run_scan` on the request thread.
- `_jobs_loop` / `run_pending_jobs` executes `run_scan` in try/except. Probe exceptions → `job.error` + Journal (`discovery` / `scan`) when they escape the scan; per-host probe failures are logged and skipped so one SNMP GET cannot abort the job. uvicorn stays up.
- HTTP always **redirects** with a flash (`Scan queued…`). Duplicate clicks reuse the pending/running row.
- Layout: **Save & scan** | **Scan now** 50/50 in `.discovery-scan-actions`; Scan card | NetBox **50/50** (`.discovery-actions`). Long probe copy lives in ⓘ. CSS `app.css?v=disc-500`.
- `docker-compose.yml` mounts `config/forgesre.yml` **rw** so Save & scan can persist `discovery.cidrs` (`:ro` was EROFS → HTTP 500). `scan_plan` no longer walks a `/8` just to count hosts.
- YAML ∪ auto-detect (real prefixes) is unchanged. No Celery. One worker thread.

Do not revert ⓘ tooltips, nav clock/resources, `/ops` report-job row actions, or `.env` / `secrets.example.env` **service-group** comments.

---

## 4. What N should do on the VM

Do **not** run `./install.sh`.

```bash
git pull origin main && ./forgesre update
```

Open **Discovery** (hard-refresh). **Save & scan** / **Scan now** should return immediately (flash: queued), not a black 500. Candidates appear after the job finishes; `./forgesre jobs` shows `discovery_scan`. **Sync NetBox** sits beside Scan now (read-only). Approve / Ignore as before; manual Assets stay SoT.

`./forgesre test` is the appliance report. `./forgesre ping` is ICMP + exporter. `./forgesre verify` is the live inventory path. Those three are different. Optional LLM: [docs/llm.md](llm.md). Jobs: **one worker thread** (no Celery). Loki: **no host logs shipped**. [architecture.md](architecture.md) is a long-term **architecture proposal**, not the V0.7 appliance runtime.

NetBox: keep the **full v2 token** (shown once at create, not the 12-character key) in `NETBOX_API_TOKEN`. Recreate **core** only if secrets changed. Never print the token.

```bash
docker compose logs netbox | grep forgesre
```

Expect **`skipping v1 upsert`** (v2 already in secrets) or **`v1 token ready`** (legacy 40-char). Not **`could not upsert`** for a valid v2 secret.

---

## 5. Product facts not to redo

- Discovery **Scan now** = YAML `discovery.cidrs` ∪ auto-detected connected IPv4 nets (real prefixes). Not `/24`-only. Not nmap. Approve still required in semi-automatic mode.
- Discovery **Save & scan** / **Scan now** enqueue Postgres `discovery_scan`. Never run `run_scan` inline on the UI request. No Celery.
- Discovery candidate booleans (`snmp_ok`, `node_exporter`, `windows_exporter`) migrate with `BOOLEAN DEFAULT FALSE`. Postgres rejects `DEFAULT 0`.
- Two files: `.env` (deployment at repo root) vs `secrets/secrets.env` (secrets). Do not merge.
- Example comments stay **English** and **grouped by service**.
- `secrets.env` values are **plaintext** env for containers. Do not claim they are hashes.
- UI users: bcrypt in `users.password_hash` only. Seed uses `FORGESRE_ADMIN_PASSWORD` once.
- Bundled **NetBox** is a **default** compose service (`:8001`). Do not put it behind a profile. Prefer a NetBox UI **v2** token (full `nbt_…`) in `NETBOX_API_TOKEN`; Core sends `Authorization: Bearer`. `scripts/netbox-upsert-token.py` **skips** v1 create for that secret. A 40-character **v1 plaintext** value is still upserted as fallback (`write_enabled=False`) on the **superuser**. Success log: **`skipping v1 upsert`** or **`v1 token ready`**. **`could not upsert`** means a v1 secret never landed in the NetBox DB (UI still starts; Discovery 403 is honest). Never touch `User.is_staff` (removed in NetBox 4.5). Core is **not** a NetBox UI login. Do not drop database `forgesre` to “fix” NetBox. Health NetBox tile: UI up + 403 = **warn**, not paused. HTTP 200 with v2 is yellow (0 devices) / green (≥1).
- NetBox UI **API token peppers**: v4.5+ needs `API_TOKEN_PEPPERS` / `NETBOX_API_TOKEN_PEPPER`. Official image reads `API_TOKEN_PEPPER_1`.
- Discovery **Sync NetBox**: Devices API 403 is a warning (rejected / recreate), not “token missing” when a secret is present. Status chip: Not connected / API 403 / No devices / Connected — grey/yellow/green. Sentence *No devices yet; add in NetBox UI `:8001` or use Assets/Discovery.* stays on yellow. **Admin only** for engineer/analyst; viewers cannot open `/discovery`.
- SNMP with no Network device + IP is **paused (no SNMP targets)** (yellow — OK).

---

## 6. How to continue next session

1. `git pull origin main`.
2. Read **this file**, then the [docs index](README.md).
3. On the VM: `git pull origin main && ./forgesre update`. Keep the full v2 token in `NETBOX_API_TOKEN`. `docker compose logs netbox | grep forgesre` should say **skipping v1 upsert** (v2) or **v1 token ready** (fallback), not could not upsert for a valid v2 secret. Never `./install.sh`. Never print tokens.
4. Branch pattern `cursor/<name>-05f8` (or task suffix). Prefer `git merge --no-ff` to `main` when PR create is 403. Replies to N are in **Serbian**. OSS docs and code stay in **English**.
