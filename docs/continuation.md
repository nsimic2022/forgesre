# Session handoff — 24 August 2026

This file is a **session handoff for the next coding agent or contributor**. It is not an operator manual. Operators start at [install and config](install-config.md) and the [operator handbook](operator-handbook.md).

Product on `main` at the end of this session: **V0.7**. Repository: https://github.com/nsimic2022/forgesre.

1. [Who and when](#1-who-and-when)
2. [Checked twice (pytest)](#2-checked-twice-pytest)
3. [Done today / on main](#3-done-today--on-main)
4. [What N should do on the VM tomorrow](#4-what-n-should-do-on-the-vm-tomorrow)
5. [Product facts not to redo](#5-product-facts-not-to-redo)
6. [How to continue next session](#6-how-to-continue-next-session)
7. [Out of scope](#7-out-of-scope)
8. [Known leftovers](#8-known-leftovers)

---

## 1. Who and when

**Monday 24 August 2026.** Operator N ran a long cloud-agent coding session against this repository. Everything listed in §3 is already merged to **`main`**. Do not treat unmerged feature branches as shipped — as of this handoff, remote feature branches that still exist are already merged (see §8).

This handoff file was rewritten at the end of the day after a double pytest on latest `main` (`1eb56e8` Merge `cursor/fix-backup-snmp-update-05f8`, then this file). No product-code fix was required; the listed facts below were verified on disk.

On the Ubuntu VM N uses, resume with:

```bash
git pull origin main
./forgesre update
```

Hard-refresh the browser after UI/CSS changes (`/static/app.css` is not cache-busted).

**Never** re-run `./install.sh` on a live box. That regenerates passwords in `secrets/secrets.env` and will wipe the install admin the operator already uses.

---

## 2. Checked twice (pytest)

On this handoff session, from latest `main`:

```bash
pip install -r requirements-dev.txt
PYTHONPATH=backend:agents python3 -m pytest tests
PYTHONPATH=backend:agents python3 -m pytest tests
```

Both runs: **189 passed**, 1 warning (Starlette `httpx` / `starlette.testclient` deprecation — ignore), ~30s each. Python 3.12.3, pytest 9.1.1.

Sanity (not a rewrite; no bugs that needed a code fix):

- `backend/app/backup.py` does **not** import sqlalchemy at module load. Host dump uses `docker compose exec -T postgres psql`. Core GUI still uses SQLAlchemy inside functions. Do **not** `pip install sqlalchemy` on the Ubuntu host.
- `snmp-exporter` is a **default** compose service (no profile), `127.0.0.1:9116`, host network. `./forgesre update` runs `up -d snmp-exporter`. Doctor checks snmp via Core `GET /api/v1/system/doctor` → curl exporter `/metrics`; connection refused is a real down service, not a silenced check.
- Assets CRUD, reachability colors, `exporter_detect`, HTML incident/escalation mail, Windows `:9182` are all on `main`.

If pytest fails next session: fix on a `cursor/<name>-05f8` branch, re-run **twice**, then `git merge --no-ff` to `main`. Do not skip a failing test.

---

## 3. Done today / on main

### HTML incident / escalation mail

Multipart **HTML + plain text** for incident reports and escalation notices.

- Modules: `backend/app/email_html.py` (shared shell), `email_service.py` (SMTP + MIME), `incident_report_mail.py`, `notifications.py`.
- `mailhtml.py` is **gone**. Do not recreate it.
- `/ops` compose (manual send + scheduled reports) stays **plain text**. HTML those later only if N asks.
- Outbox still stores the plain body.

### Windows Server + windows_exporter :9182

- Linux: **node_exporter `:9100`**.
- Windows: **windows_exporter `:9182`**.
- Seeded `forge-demo-win-01` (`10.10.10.21`) is **lab-only**, not scraped. Real Windows hosts need windows_exporter on the host and TCP 9182 from this VM.

### Auto OS detect (Assets / Discovery)

`backend/app/exporter_detect.py` GETs `/metrics` on `:9182` then `:9100` (short timeout).

- `windows_exporter` / `windows_` → Windows Server, `windows-standard`, `<ip>:9182`.
- `node_exporter` / `node_` (`node_uname` / `node_cpu`) → Linux Server, `linux-standard`, `<ip>:9100`.
- **Both:** keep a saved Linux/Windows type if the row already has one; otherwise prefer Windows `:9182` (mis-classifying Windows as Linux was the scrape miss).
- **Network is not guessed** because HTTP is silent. Network only via the real SNMP path (`snmp_ok` / live UDP/161 GET). Missing `:9100`/`:9182` is not a fingerprint.
- ICMP ping is a reachability hint only — it does not pick an OS.

### `./forgesre ping` / `probe`

ICMP **plus** exporter HTTP `/metrics` from this VM. Alias: `probe`.

- Ping ≠ scrape. Do not add a ping-only “host up” incident.
- Skips seeded `forge-demo-*` unless `--demo` or the id is passed.
- Inventory already in ForgeSRE; no extra flags for the common case.

### Assets Add / Edit / Clone / Remove

- List page: Add, Edit, Clone, Remove. Optional scrape address (`ip:9100` or `ip:9182`).
- Cannot delete `forge-demo-*` (lab hosts used by dashboard demos / similar-incident history). Clone strips the demo hostname so the copy is a real row.
- Removing a real asset drops HTTP/SNMP SD targets. **Incidents stay in history**; the asset FK is cleared.

### Assets table: ping / exporter colors

Green / yellow / red pills in the **Ping / comms** column (already on main; made easier to see). Dashboard: one HOST DOWN banner lists open `NodeExporterDown` / `WindowsExporterDown` / `SnmpDeviceUnreachable` incidents (link + DEMO).

- Last-known colors on first HTML paint; yellow until the first probe.
- Background `GET /api/v1/assets/reachability` refreshes them. Do **not** probe inside the HTML list handler.

### Theme

- Left nav is a **constant dark** RH-ish shell.
- Content pane cycles **light → dark → system** (OS `prefers-color-scheme`). Default light. Persist `forgesre-theme`.
- **High-contrast is removed.** Stored `high-contrast` is treated as dark.
- `/static/app.css` is not cache-busted — operators must hard-refresh after CSS changes.

### Backup / import

- Administration **before** Appliance shell on `/admin`. Archives: `data/backups/*.tar.gz` (gitignored, mode 700/600).
- `./forgesre backup` / `./forgesre restore`. Secrets are in the archive by default; admin-only. Skip GGUF by default (`--include-models` / UI checkbox to include).
- Restore needs typed `RESTORE` (UI) or `--yes` (CLI). `./forgesre restore` without `--yes` prints the plan and exits 1.
- Not in the archive: Docker images, Prometheus/Loki/Grafana volumes, nested backups, optional mailbox mail.

### Appliance web PTY not shipped

Administration does **not** open a terminal in the browser. A web PTY (even wrapped to `./forgesre`) is a host command channel if an admin cookie is stolen. Operators use SSH + `./forgesre` / `./forgesre shell`.

### `update` backup crash (sqlalchemy on host)

Symptom N hit: `./forgesre backup` on **host** Python imported sqlalchemy at `backup.py` line 24 → `ModuleNotFoundError`. `update` uses `set -e`, so compose never came up and snmp-exporter stayed down (`127.0.0.1:9116` connection refused, doctor **DEGRADED**).

Fix already on `main` (`cursor/fix-backup-snmp-update-05f8`):

- Host dump via `docker compose exec postgres` (JSON agg / `json_populate_recordset`). Same `db.json` tar format.
- Core GUI still uses sqlalchemy.
- Backup failure on update is a clear error (no traceback); update **continues** with render-monitoring and compose up, plus `up -d snmp-exporter`.

**Do not pip install sqlalchemy on the Ubuntu host.** That is not a product fix. Host CLI must stay stdlib-only (no sqlalchemy, no PyYAML).

### snmp-exporter default stack

Default compose service (not a profile). `update` starts it. Doctor `:9116`. Connection refused was because update died on backup **before** compose up — start the service, do not silence the check. Do not move snmp-exporter behind a profile. Do not require Zabbix.

---

## 4. What N should do on the VM tomorrow

Do **not** run `./install.sh`.

```bash
cd ~/forgesre    # or wherever the clone lives
git pull origin main
./forgesre update
./forgesre doctor
./forgesre snmp
```

Then:

1. Hard-refresh the browser (CSS/theme).
2. Assets: confirm ping / `:9100` / `:9182` / SNMP dots; Edit / Clone / Remove still there.
3. If snmp is still ✗: `docker compose up -d snmp-exporter` then `./forgesre snmp` / `./forgesre logs snmp-exporter`.
4. Optional: `./forgesre ping` against a real host (not only ICMP).
5. Optional: `./forgesre test` → `data/reports/`.

If doctor is HEALTHY and snmp is ok, the morning is done. New product work starts from this file + [llm.md](llm.md) + [cli.md](cli.md).

---

## 5. Product facts not to redo

These already work on `main`. Do not “fix” them unless N asks.

- Theme toggle cycles **light → dark → system**. Left nav stays dark. **System** follows OS for the content area only.
- Dashboard demos are **one** top-right button + a closeable panel. Demo rows stay visible and **labeled DEMO**.
- Incident ids look like `INC-0134_16.08.2026_09:13`. Older `INC-000012` rows stay valid.
- RCA is Python under `agents/rca/`. The LLM only rewrites prose. Builtin ForgeRCA always runs first.
- Core is an SMTP **client** only. The UI has no IMAP inbox.
- pytest is a laptop/dev dependency. The Core image must not install it.
- Real Windows scrape is **windows_exporter :9182**, not the lab demo host.
- Auto-detect is a helper + defaults, not a new fingerprinting subsystem.
- Host CLI must not require sqlalchemy/PyYAML.
- `snmp-exporter` is a **default** compose service.

---

## 6. How to continue next session

1. `git pull origin main` (or fetch and check out `main`). Code lives there.
2. Read **this file**, then [`docs/llm.md`](llm.md) and [`docs/cli.md`](cli.md).
3. On the VM N uses: `git pull origin main && ./forgesre update`, then `./forgesre snmp` and `./forgesre test`. Never `./install.sh` on that box.
4. Developer checks: `pip install -r requirements-dev.txt` if needed, then `PYTHONPATH=backend:agents python3 -m pytest tests` **twice**, then merge to `main`. New work uses branch pattern `cursor/<name>-05f8`.
5. Replies to N are in **Serbian**. OSS docs and code stay in **English**.
6. `ManagePullRequest` `create_pr` / `update_pr` often 403. `git merge --no-ff` plus `git push origin main` still lands the change.

---

## 7. Out of scope

Do not start these unless N asks:

- Do **not** implement the longer-term Go / Kubernetes rewrite in [`docs/architecture.md`](architecture.md). That document is a design note, not a sprint.
- Do **not** implement Zabbix templates, or ticketing as a second Ticket object (Jira, TheHive, or an in-app ticket thread). Incident notes are short operator comments, not tickets.
- Do **not** add an IMAP inbox in the UI. Core sends mail; replies land in the real mailbox.
- Do **not** enable Compose profile `mailbox` by default. It stays off until `./forgesre mailbox`. That command does not rewrite Gmail/Outlook SMTP unless `--bind-core`.
- Do **not** change the default `./forgesre fetch-llm` URL from Qwen2.5-14B-Instruct Q4_K_M to 4B unless N asks.
- Do **not** put a GGUF (or any model weights) in git.
- Do **not** add React, Tailwind, Bootstrap, PatternFly npm, or a new icon pack.
- Do **not** fake a live Windows scrape or SNMP walk in the demo panel.

---

## 8. Known leftovers

Not holes to fill on sight:

- Many remote `origin/cursor/*-05f8` branches still exist and are **already merged to `main`**. Safe to ignore; do not reopen them. Cleaning old remote branches is optional hygiene, not product work.
- `curl /health` is documented in [`docs/llm.md`](llm.md). It is not duplicated in every Advanced CLI list (`docs/install-config.md` §13 points at llm.md §8).
- The GitHub README `fetch-llm` line does not inline the 8 GB wget. The lab wget lives in [`docs/llm.md`](llm.md) §3.C.
- Job claim could later use `FOR UPDATE SKIP LOCKED` on Postgres. Do not pretend it already does. SQLite tests would need a fallback.
- `incident_detail.html` / `ai.html` RCA markup is similar but not identical. Extract a partial only if both pages should look the same.
- Scheduled performance reports on `/ops` are still plain text. HTML them only if N asks.
- Existing Windows hosts added as `Linux Server` keep `:9100` until Detect or a type/scrape edit. No automatic rewrite of custom scrape addresses on every save.
- snmp-exporter image healthcheck uses busybox `wget`. If a future image is distroless, drop the healthcheck; doctor still curls `:9116`.
