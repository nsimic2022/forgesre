# Session handoff — 24 August 2026

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

**Monday 24 August 2026 (late evening).** Operator N hit two bugs on the Ubuntu VM:

1. `./forgesre verify` crashed with `ModuleNotFoundError: sqlalchemy` (same class as the old host `backup` crash: `asset_verify` → `seed` → ORM).
2. He wanted each backup run in its **own folder** so `data/backups/` does not look like a pile of files he cannot tell how to import. He is OK keeping backup-on-update as a safety net. CLI `./forgesre backup` and GUI Backup/Import stay.

On the Ubuntu VM N uses, resume with:

```bash
git pull origin main
./forgesre update
```

Hard-refresh the browser after UI/CSS changes (`/static/app.css` is not cache-busted).

**Never** re-run `./install.sh` on a live box. That regenerates passwords in `secrets/secrets.env` and will wipe the install admin the operator already uses.

---

## 2. Checked twice (pytest)

From this branch, after `pip install -r requirements-dev.txt`:

```bash
PYTHONPATH=backend:agents python3 -m pytest tests
PYTHONPATH=backend:agents python3 -m pytest tests
```

Both runs: **217 passed**, 1 warning (Starlette `httpx` / `starlette.testclient` deprecation — ignore), ~32s each. Python 3.12, pytest 9.x.

If pytest fails next session: fix on a `cursor/<name>-05f8` branch, re-run **twice**, then `git merge --no-ff` to `main`.

---

## 3. Done today / on main

### Host `./forgesre verify` without sqlalchemy

`is_demo_asset_id` moved to `backend/app/demo_ids.py` (stdlib only). `seed.py` re-exports it for Core. Host CLI modules (`asset_verify`, `cli_ops`, `cli_view`) import the helper, not `app.seed`. Do **not** pip-install sqlalchemy on Ubuntu.

- `./forgesre verify` / `ping` / `incidents` on the host.
- GUI Verify still runs inside Core (sqlalchemy is fine there).
- Test: importing `asset_verify` and `cli_ops` with sqlalchemy blocked.

### Backup layout: one folder per run, one tar to import

```
data/backups/backup_YYYYMMDDTHHMMSSZ/forgesre.tar.gz
data/backups/backup_YYYYMMDDTHHMMSSZ/MANIFEST.txt
```

Dirs mode `700`, archives mode `600`. gitignore unchanged (`data/`). Restore/import accept the folder or the tar inside it. Legacy `data/backups/forgesre-*.tar.gz` at the root still lists and restores. Staging temp is not left next to the tar.

- `./forgesre backup` still exists (CLI + GUI).
- `./forgesre update` still runs backup after doctor as a safety net (DEGRADED doctor may continue; backup is not blocked on snmp).
- Administration Restore already had a folder dropdown: it now lists `backup_*` dirs newest first with timestamp + name (not tar contents). `./forgesre restore` / `import` with no path prints a numbered picker; still needs `--yes`.

---

## 4. What N should do on the VM

Do **not** run `./install.sh`.

```bash
cd ~/forgesre
git pull origin main
./forgesre update
```

Then:

```bash
./forgesre verify
./forgesre backup
ls -la data/backups/
```

Hard-refresh Administration if you use GUI Backup/Import.

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
- Host CLI must not require sqlalchemy/PyYAML. Do not `pip install sqlalchemy` on the Ubuntu host.
- `snmp-exporter` is a **default** compose service.
- Dashboard **HOST DOWN** banner (open exporter/SNMP-down incidents). Do not redo it.
- Backup on the host dumps Postgres via `docker compose exec postgres`.
- One restore unit = one `.tar.gz` inside `backup_<stamp>/`. Do not explode the archive into loose files at `data/backups/` root.

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

---

## 8. Known leftovers

- Many remote `origin/cursor/*-05f8` branches still exist and are **already merged to `main`**.
- Core slim image may not include `ping`; GUI ICMP then shows that honestly. `./forgesre verify` probes ICMP from the **host** (same as `./forgesre ping`).
- Scheduled `/ops` reports are still plain text.
- Old backups already on the VM as `data/backups/forgesre-*.tar.gz` are still valid; new runs write folders.
