# Session handoff — 27 August 2026

This file is a **session handoff for the next coding agent or contributor**. It is not an operator manual. Operators start at [install and config](install-config.md) and the [operator handbook](operator-handbook.md).

Product on `main` at the end of this session: **V0.7**. Repository: https://github.com/nsimic2022/forgesre.

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

**Thursday 27 August 2026.** Ordered review fixes on top of bundled-NetBox `main`: docs matched to code, doctor probes (not dummy ok), RCA Loki honesty, LLM worker no longer holds `/ops` reports for 600s, `update.sh` skip Core `--build` when sources unchanged.

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

Both runs: **328 passed**, 1 warning (Starlette `httpx` / `starlette.testclient` deprecation — ignore), ~40s each. Python 3.12, pytest 9.x.

If pytest fails next session: fix on a `cursor/<name>-05f8` branch, re-run **twice**, then `git merge --no-ff` to `main`. Branch pattern `cursor/<name>-05f8`.

---

## 3. Done today / on main

1. **Docs = code.** `docs/cli.md` everyday `update` includes NetBox `:8001`, first-boot yellow, wait. `docs/v0.7.md` bundled NetBox **is** on this V0.7 main. Handbook §17 bundled alerts include Linux+Windows **memory 90%**; Grafana is **not** the alarm path. `CONTRIBUTING.md` V0.7. Example YAML `ai.enabled: false` (standard install). The lab SMTP catcher stays gone. `docs/architecture.md` one line: appliance runtime is Compose, not the Caddy/Go diagram. Install troubleshooting: Redis `:6379`, NetBox first boot, mailbox 25/993.
2. **Doctor.** Stack `core` probes the same `/api/v1/health` as `doctor.sh` (label **Core (container)**; CLI curl stays **Core API**). `discovery` probes last journal scan + loop heartbeat — not a checkbox. SNMP still **paused** with no network targets. NetBox first-boot stays **warn**.
3. **RCA Loki.** Alloy labels Core logs `asset=forge-demo-01`. Real inventory does not get empty `{asset="<id>"}` presented as host logs. Limitation: “no host logs shipped”. Demo may still query appliance logs, labeled DEMO.
4. **LLM vs `/ops` reports.** Default `ai.llm.timeout_seconds` is **90** (lab 4B). Job loop runs `process_scheduled_reports` **before** pending jobs and prefers non-LLM investigate jobs. Builtin RCA path unchanged. No Celery / SKIP LOCKED.
5. **`update.sh`.** `compose pull` only when not `--offline`. Skip Core `--build` when Dockerfile/backend/agents/frontend hash is unchanged. NetBox wait on first boot stays.

---

## 4. What N should do on the VM

Do **not** run `./install.sh`.

```bash
git pull origin main && ./forgesre update
```

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
- Bundled **NetBox** is a **default** compose service (`:8001`). Do not put it behind a profile. `--netbox-url` remains an external override.
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

---

## 8. Known leftovers

- Many remote `origin/cursor/*-05f8` branches still exist and are **already merged to `main`**.
- Core slim image may not include `ping`; GUI ICMP then shows that honestly. `./forgesre verify` probes ICMP from the **host** (same as `./forgesre ping`).
- Scheduled `/ops` reports are still plain text.
- Old backups already on the VM as `data/backups/forgesre-*.tar.gz` are still valid; new runs write folders.
- Grafana deep-link from an asset is still later.
- Prometheus global rules may still fire for a host whose ForgeSRE alarm is disabled or raised; ForgeSRE will not open the incident when the webhook carries the value.
- Alloy still only ships appliance Core logs as `forge-demo-01`. Real hosts have no Loki until that changes.
- LLM rewrite can still occupy the single job loop for up to `timeout_seconds` (default 90) after reports in that pass have already run.
- `config/forgesre.yml` on a live VM is gitignored; a 600s timeout already written there is not overwritten by `update`. Lower it by hand if the worker still blocks.
