# Session handoff — 7 September 2026

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

**Monday 7 September 2026.** Operator N (Serbian): they **did** put `NETBOX_API_TOKEN` in `secrets/secrets.env`, but Discovery still showed **API 403** and the long sentence still said “token missing, not created in NetBox…”. That copy is wrong when Core already has a token and NetBox rejects it (HTTP 403). They must recreate **core** (and netbox) after editing secrets. Do **not** print the token. Do **not** require a second token from the NetBox UI. Do **not** revert the v1 upsert.

Code and docs stay English. Replies to N are Serbian.

**Never** re-run `./install.sh` on a live box. That regenerates passwords in `secrets/secrets.env` and will wipe the install admin the operator already uses.

---

## 2. Checked twice (pytest)

From this branch, after `pip install -r requirements-dev.txt`:

```bash
PYTHONPATH=backend:agents python3 -m pytest tests
PYTHONPATH=backend:agents python3 -m pytest tests
```

Pytest count after the double run on `cursor/netbox-403-copy-05f8`: recorded in the merge commit. Was 381 after the status-chip fix.

If pytest fails next session: fix on a `cursor/<name>-05f8` branch, re-run **twice**, then `git merge --no-ff` to `main`. Branch pattern `cursor/<name>-05f8`. `create_pr` often **403** — merge `--no-ff` plus `git push origin main` still lands the change.

---

## 3. Done today / on this branch

Discovery/doctor 403 copy now depends on whether Core has a token (yes/no only, never the value):

- Token **present** → **NetBox rejected the API token (HTTP 403). Recreate netbox+core after changing secrets.env.**
- Token **empty** → keep the missing-token sentence (`NETBOX_API_TOKEN is empty` / token missing).
- Chip label stays **API 403**. Sync NetBox stays the only action.

Core reads `NETBOX_API_TOKEN` from bind-mounted `secrets/secrets.env` (`FORGESRE_SECRETS_FILE=/host-secrets.env`). Compose does **not** interpolate `${NETBOX_API_TOKEN}` into Core `environment` (empty/stale `.env` must not override). If the secrets file exists, Settings uses that file even when the key is empty — process env / `.env` cannot override it.

Doctor and Discovery show `API token: yes` or `API token: no` only.

---

## 4. What N should do on the VM

Do **not** run `./install.sh`. Do **not** paste a NetBox UI token (or the 12-character **key prefix**) into `secrets.env`.

```bash
git pull origin main && ./forgesre update
```

That recreates **netbox** and **core**. After editing `secrets/secrets.env` they **must** recreate **core** (bind mount is read at runtime, but env_file and NetBox launch still need a recreate):

```bash
docker compose up -d --no-deps --force-recreate netbox
docker compose up -d --no-deps --force-recreate core
```

Hard-refresh Discovery (Ctrl-Shift-R). Chip = **status** (Not connected / API 403 / No devices / Connected). **Sync NetBox** = click. Footer shows `API token: yes` or `no` — never the secret.

If **API token: yes** and still 403, they likely pasted the NetBox **key prefix** (12 chars) instead of the full token shown once at create, or **core was not recreated**. curl the devices API (do not print the token):

```bash
set -a && source secrets/secrets.env && set +a
code=$(curl -sS -o /dev/null -w '%{http_code}' \
  -H "Authorization: Token ${NETBOX_API_TOKEN}" \
  http://127.0.0.1:8001/api/dcim/devices/?limit=1)
echo "$code"
```

**200** = Core can sync. **403** with token present = NetBox rejected it (wrong prefix, or netbox not recreated so the v1 upsert did not run).

Lab without image pull: `./forgesre update --offline`.

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
- `snmp-exporter` is a **default** compose service. No SNMP targets → doctor **paused** (yellow), not DOWN.
- Bundled **NetBox** is a **default** compose service (`:8001`). Do not put it behind a profile. `--netbox-url` remains an external override. Image pin is `netboxcommunity/netbox:v4.6.9-5.0.2`. Do not churn NetBox Hub/GHCR tags unless N asks. Core sync token is `NETBOX_API_TOKEN`; launch upserts it as a **v1 plaintext** token (`write_enabled=False`) on every start. N does **not** need a second UI token. Do not drop database `forgesre` to “fix” NetBox.
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
- Discovery **Sync NetBox**: primary (orange) admin POST when the UI answers (`/login/` or `/api/status/`). Devices API 403 is a warning, not `disabled`. First-boot (UI down) stays disabled with one sentence. Engineer/analyst see disabled + **Admin only.** Viewers cannot open `/discovery`. Never write back to NetBox. Bundled footer must not say “external instance”. Launch still upserts the v1 token; `update` `--force-recreate`s `netbox`. Do not require a second UI token. The chip next to Sync is status only (Not connected / API 403 / No devices / Connected) — grey/yellow/green CSS, not a second button. Sentence *No devices yet; add in NetBox UI `:8001` or use Assets/Discovery.* stays on yellow. 403 with a token present says rejected (recreate netbox+core), not “token missing”.

Also: `./forgesre ping` and `./forgesre verify` stay distinct from `./forgesre test` / doctor. See [`docs/llm.md`](llm.md).

---

## 6. How to continue next session

1. `git pull origin main`.
2. Read **this file**, then [`docs/llm.md`](llm.md) and [`docs/cli.md`](cli.md).
3. On the VM: `git pull origin main && ./forgesre update`. Never `./install.sh`. Never a second NetBox UI token for Core. Recreate **core** after editing `secrets.env`.
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
- Scheduled `/ops` reports are still plain text.
- Old backups already on the VM as `data/backups/forgesre-*.tar.gz` are still valid; new runs write folders.
- Grafana deep-link from an asset is still later.
- Prometheus global rules may still fire for a host whose ForgeSRE alarm is disabled or raised; ForgeSRE will not open the incident when the webhook carries the value.
- Alloy still only ships appliance Core logs as `forge-demo-01`. Real hosts have no Loki until that changes. Limitation: **no host logs shipped**.
- LLM rewrite can still occupy the single job loop for up to `timeout_seconds` (default 90 in example.yml; N’s live yml may be 300) after reports in that pass have already run.
- `config/forgesre.yml` on a live VM is gitignored; a 300s or 600s timeout already written there is not overwritten by `update`.
- Loki/Alloy still FAIL doctor when those containers are dark (not Grafana). Only Grafana was moved to warn.
