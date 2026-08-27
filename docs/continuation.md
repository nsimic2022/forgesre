# Session handoff — 27 August 2026

This file is a **session handoff for the next coding agent or contributor**. It is not an operator manual. Operators start at [install and config](install-config.md) and the [operator handbook](operator-handbook.md).

Product on `main` at the end of this session: **V0.7**. Repository: https://github.com/nsimic2022/forgesre.

Compose image re-audit (registry-1.docker.io + GHCR manifests, 27 Aug 2026). Dead pin was **NetBox** `netboxcommunity/netbox:v4.4-3.2.0` (404; that combo never existed — docker-support `3.2.0` was NetBox 4.2 / nginx unit). Current pin is **`ghcr.io/netbox-community/netbox:v4.6.9-5.0.2`** (same digest as Hub `netboxcommunity/netbox:v4.6.9-5.0.2`, Granian, `user: netbox:root`). GHCR avoids Hub anonymous rate-limits that look like “manifest unknown”. Redis **`redis:7-alpine`** still exists (200). Postgres stays **`postgres:16-alpine`** (database `netbox` beside `forgesre` — do not bump to 18). Other default and mailbox pins still exist. `./forgesre update` now **fails** if `compose pull` 404s (no more `pull || true`). Lab: `./forgesre update --offline`.

1. [Who and when](#1-who-and-when)
2. [Checked twice (pytest)](#2-checked-twice-pytest)
3. [Done today / on main](#3-done-today--on-main)
4. [What N should do on the VM](#4-what-n-should-do-on-the-vm)
5. [Product facts not to redo](#5-product-facts-not-to-redo)
6. [How to continue next session](#6-how-to-continue-next-session)
7. [Out of scope](#7-out-of-scope)
8. [Known leftovers](#8-known-leftovers)

---

## 1. Who and when

**Thursday 27 August 2026.** Second compose image pass: N still reported a missing NetBox Hub tag. Re-verified every default/mailbox/AI pin against the registry API (no Docker daemon in this lab). Switched NetBox pull to GHCR (same digest as Hub). `update.sh` no longer swallows pull 404s.

On the Ubuntu VM N uses, resume with:

```bash
git pull origin main && ./forgesre update
```

Lab without image pull: `./forgesre update --offline`. (`./forgesre update` already runs `render-monitoring` and waits for NetBox `/login/`.) Also: `./forgesre ping`, `./forgesre verify`, `./forgesre test` (appliance health, not inventory). See [docs/llm.md](llm.md).

Hard-refresh the browser after UI/docs changes.

**Never** re-run `./install.sh` on a live box. That regenerates passwords in `secrets/secrets.env` and will wipe the install admin the operator already uses.

---

## 2. Checked twice (pytest)

From this branch, after `pip install -r requirements-dev.txt`:

```bash
PYTHONPATH=backend:agents python3 -m pytest tests
PYTHONPATH=backend:agents python3 -m pytest tests
```

Pytest count is recorded after the double run on this branch.

If pytest fails next session: fix on a `cursor/<name>-05f8` branch, re-run **twice**, then `git merge --no-ff` to `main`. Branch pattern `cursor/<name>-05f8`.

---

## 3. Done today / on main

1. **NetBox image.** Hub `netboxcommunity/netbox:v4.4-3.2.0` = 404. Compose now pulls **`ghcr.io/netbox-community/netbox:v4.6.9-5.0.2`** (manifest 200; digest `sha256:6b0594813c1e…` matches Hub `v4.6.9-5.0.2`). Granian + `user: netbox:root` in `scripts/netbox-launch.sh` still match. First boot wait on `:8001/login/` unchanged.
2. **Redis / others.** `redis:7-alpine` exists (amd64/arm64). Left as-is. Did **not** switch to Valkey (upstream netbox-docker 5.0.2 uses Valkey; Redis 7 still speaks the protocol NetBox expects). Postgres stays `16-alpine` so the shared volume / `forgesre` DB is not rewritten. Mailbox overlay still `docker-mailserver:15.1.0` + Roundcube `1.6.11-apache`. Prometheus/Grafana/Loki/Alloy/Alertmanager/snmp-exporter/llama.cpp/python:3.12-slim all still exist. No Caddy service in V0.7 compose (architecture.md only).
3. **`update.sh`.** `docker compose pull || true` hid 404s, then `up --pull never` looked like “image does not exist”. Pull now fails the update with a pointer at git-tracked `docker-compose.yml`. `--offline` still skips pull.
4. **Docs.** Handbook + install troubleshooting name the GHCR pin. Live `config/forgesre.yml` is gitignored and does **not** set image tags; `./forgesre update` after `git pull` picks up compose. Test `test_compose_and_mailbox_image_pins` lists every compose `image:` and checks docs match.

---

## 4. What N should do on the VM

Do **not** run `./install.sh`.

```bash
git pull origin main && ./forgesre update
```

This update **must** pull images (NetBox moved Hub → GHCR, same version). First NetBox pull is slow; doctor stays yellow until `:8001/login/` answers.

Hard-refresh the browser.

---

## 5. Product facts not to redo

These already work on `main`. Do not “fix” them unless N asks.

- `./forgesre test` = appliance health report → `data/reports/`. `./forgesre verify` = live inventory communication.
- Theme toggle cycles **light → dark → system**. Left nav stays dark.
- Dashboard demos are **one** top-right button + a closeable panel. Demo rows stay **labeled DEMO**.
- Incident ids look like `INC-0134_16.08.2026_09:13`.
- RCA is Python under `agents/rca/`. The LLM only rewrites prose. Builtin ForgeRCA always runs first.
- Core is an SMTP **client** only. The UI has no IMAP inbox.
- pytest is a laptop/dev dependency. The Core image must not install it.
- Real Windows scrape is **windows_exporter :9182**, not the lab demo host.
- Prometheus Health Open is **Targets** (`:9090/targets?search=`), not Prometheus process `/metrics`. Core `/metrics` stays.
- Host CLI must not require sqlalchemy/PyYAML. Do not `pip install sqlalchemy` on the Ubuntu host.
- `snmp-exporter` is a **default** compose service.
- Bundled **NetBox** is a **default** compose service (`:8001`). Do not put it behind a profile. `--netbox-url` remains an external override. Image pin is `ghcr.io/netbox-community/netbox:v4.6.9-5.0.2` (not Hub `v4.4-3.2.0`). Redis `redis:7-alpine` on `:6379`.
- Dashboard **HOST DOWN** banner (open exporter/SNMP-down incidents). Do not redo it.
- Backup on the host dumps Postgres via `docker compose exec postgres` with the same docker rights as `./forgesre update` (`docker info`, else `sudo docker compose`). A docker.sock permission error is not “start postgres”.
- One restore unit = one `.tar.gz` inside `backup_<stamp>/`. Do not explode the archive into loose files at `data/backups/` root.
- Host `./forgesre verify` does not import sqlalchemy (`demo_ids.py`).
- Memory bundled alerts exist: `NodeMemoryHigh` / `WindowsMemoryHigh` at **90%**, playrules `node-memory` / `windows-memory`. Grafana is not the alarm path.
- Add asset: operator types **Asset ID** and **Hostname** separately. Id is immutable after create. Do not derive id from hostname again.
- Doctor labels: **Core API** (`doctor.sh` health curl) vs **Core (container)** (payload `/api/v1/health` probe). Do not rename them back to a single “Core”.
- The lab SMTP catcher is gone. Do not add one.

---

## 6. How to continue next session

1. `git pull origin main`.
2. Read **this file**, then [`docs/llm.md`](llm.md) and [`docs/cli.md`](cli.md).
3. On the VM: `git pull origin main && ./forgesre update`. Never `./install.sh`.
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
- Switching bundled Redis to Valkey, or Postgres 16 → 18, without a migration plan.

---

## 8. Known leftovers

- Many remote `origin/cursor/*-05f8` branches still exist and are **already merged to `main`**.
- Core slim image may not include `ping`; GUI ICMP then shows that honestly. `./forgesre verify` probes ICMP from the **host** (same as `./forgesre ping`).
- Scheduled `/ops` reports are still plain text.
- Old backups already on the VM as `data/backups/forgesre-*.tar.gz` are still valid; new runs write folders.
- Grafana deep-link from an asset is still later.
- Prometheus global rules may still fire for a host whose ForgeSRE alarm is disabled or raised; ForgeSRE will not open the incident when the webhook carries the value.
- Alloy still only ships appliance Core logs as `forge-demo-01`. Real hosts have no Loki until that changes. Limitation: **no host logs shipped**.
- LLM rewrite can still occupy the single job loop for up to `timeout_seconds` (default 90) after reports in that pass have already run.
- `config/forgesre.yml` on a live VM is gitignored; a 600s timeout already written there is not overwritten by `update`. Lower it by hand if the worker still blocks.
- This lab had **no Docker daemon**. Image existence was verified via Hub/GHCR registry APIs, not `docker compose pull`.
