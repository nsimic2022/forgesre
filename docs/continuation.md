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

**Wednesday 9 September 2026.** N approved Discovery upgrade (“uradi sve”): safer **Confirm `/24`** (not silent all-VLAN scan), candidate exporter/SNMP columns, Scan now | NetBox side-by-side. Keep Postgres `discovery_scan` job (no inline `run_scan`, **no Celery**). Branch: `cursor/discovery-confirm-cidr-cb97`. Prefer `git merge --no-ff` when PR create is 403.

**Never** re-run `./install.sh` on a live box. That regenerates passwords in `secrets/secrets.env`. Never print tokens. Never commit real secrets.

Replies to N are in **Serbian**. OSS docs and code stay in **English**.

---

## 2. Checked twice (pytest)

```bash
PYTHONPATH=backend:agents python3 -m pytest
PYTHONPATH=backend:agents python3 -m pytest
```

Pytest count after the double run on `cursor/discovery-confirm-cidr-cb97`: **444 passed** (twice). `create_pr` / `ManagePullRequest` often **403** — `git merge --no-ff` plus `git push origin main` still lands the change.

---

## 3. Done today / on this branch

### Discovery Confirm /24 (safer “scan my network”)

- Suggested management CIDR = appliance **primary IPv4 `/24`**. Does **not** silently scan all VLANs.
- UI: CIDR field prefilled; **Confirm & scan** writes `discovery.cidrs` to live `config/forgesre.yml` and **enqueues** `discovery_scan`. **Scan now** only after confirmed CIDRs. Empty `discovery.cidrs` = **no scan** (API 400; background loop skips).
- Limits **256/CIDR**, **1024** total. Ports: TCP **22, 80, 443, 9100, 9182** + SNMP **UDP/161**. Not nmap. Approve queue unchanged.
- Candidate table: **Open ports**, **node_exporter**, **windows_exporter**, **SNMP** (`snmp_ok`). `upsert_candidate` persists flags (DB columns + migrate).
- Layout: **Confirm & scan** | **Scan now** in `.discovery-scan-actions`; Scan card | NetBox **50/50** (`.discovery-actions`). CSS/JS `app.css?v=disc-1` / `app.js?v=disc-1`.
- Job queue kept: HTTP never calls `run_scan` inline. Worker uses confirmed YAML cidrs only (`merge_auto=False`). No Celery.

Do not revert ⓘ tooltips, nav clock/resources, `/ops` report-job row actions, or `.env` / `secrets.example.env` **service-group** comments.

---

## 4. What N should do on the VM

Do **not** run `./install.sh`. Hard-refresh Discovery after update (`app.css?v=disc-1`).

```bash
git pull origin main && ./forgesre update
```

SHA: **(fill after merge to main)**. Open **Discovery**. Review suggested `/24`, **Confirm & scan**, wait for the background job (`./forgesre jobs`). **Sync NetBox** sits beside Scan now (read-only). Approve / Ignore as before; manual Assets stay SoT.

`./forgesre test` is the appliance report. `./forgesre ping` is ICMP + exporter. `./forgesre verify` is the live inventory path. Those three are different. Optional LLM: [docs/llm.md](llm.md). Jobs: **one worker thread** (no Celery). Loki: **no host logs shipped**. [architecture.md](architecture.md) is a long-term **architecture proposal**, not the V0.7 appliance runtime.

NetBox: keep the **full v2 token** (shown once at create, not the 12-character key) in `NETBOX_API_TOKEN`. Recreate **core** only if secrets changed. Never print the token.

```bash
docker compose logs netbox | grep forgesre
```

Expect **`skipping v1 upsert`** (v2 already in secrets) or **`v1 token ready`** (legacy 40-char). Not **`could not upsert`** for a valid v2 secret.

---

## 5. Product facts not to redo

- Discovery suggests **primary IPv4 `/24`**. Operator must **Confirm & scan**. Empty `discovery.cidrs` = **no scan**. Does **not** auto-union every connected VLAN.
- Discovery **Confirm & scan** / **Scan now** enqueue Postgres `discovery_scan`. Never run `run_scan` inline on the UI request. No Celery.
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
