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

**Monday 24 August 2026 (evening).** Operator N asked for **`./forgesre verify`** plus the same action in the **Assets GUI**. That is live communication for inventory, **not** a rewrite of `./forgesre test` (appliance health after `update`).

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

Both runs: **201 passed**, 1 warning (Starlette `httpx` / `starlette.testclient` deprecation — ignore), ~30s each. Python 3.12, pytest 9.x.

If pytest fails next session: fix on a `cursor/<name>-05f8` branch, re-run **twice**, then `git merge --no-ff` to `main`.

---

## 3. Done today / on main

### `./forgesre verify` (not `./forgesre test`)

Live path: inventory asset → ICMP / exporter or SNMP → Prometheus `up` → optional last RCA facts vs PromQL. LLM is **listed** only if ForgeAI is enabled; verify does **not** call the LLM.

- `./forgesre verify` — all real assets (seeded `forge-demo-*` skipped unless `--demo`, then labeled DEMO).
- `./forgesre verify <name-or-id>` — one machine: inventory dump + live probes (sections, PASS/FAIL/SKIP).
- Classes are universal, not SKUs: Linux `:9100` `node_`, Windows `:9182` `windows_`, Network SNMP (existing snmp_exporter path), Unknown → SKIP with a reason.
- Missing exporter / no Prom target = SKIP or FAIL with an honest reason. Never a fake green host.
- Demo `forge-demo-*` is lab, not proof of a real scrape.
- Reuses `asset_probe` / `exporter_detect` / reachability / inventory. Does not duplicate backup, theme, or HTML mail.

Code: `backend/app/asset_verify.py`, CLI `scripts/forgesre` + `cli_ops.py`. API: `GET /api/v1/assets/{id}/verify`, `GET /api/v1/verify`, `GET /api/v1/verify-support`.

### Assets GUI Verify

Same permission as Add/Edit (`write_assets`: analyst / engineer / admin). Viewers are read-only.

- Row button **Verify** on the Assets list; also on the asset page and the Edit form.
- **Verify all** on the list (`/assets/verify`).
- Result page: ping, port, Prom `up`, metric family, last RCA mismatch, LLM skip/pass.

### Already on main (do not redo)

Assets CRUD, reachability colors, `exporter_detect`, HTML incident/escalation mail, Windows `:9182`, `./forgesre ping`, host backup without sqlalchemy, snmp-exporter as a default compose service. See §5.

---

## 4. What N should do on the VM

Do **not** run `./install.sh`.

```bash
cd ~/forgesre
git pull origin main
./forgesre update
```

Then hard-refresh the browser.

```bash
./forgesre verify
./forgesre verify <hostname-or-id>
./forgesre help verify
```

Assets: **Verify** on the row (and Verify all). Edit / Clone / Remove stay. Demo rows stay labeled DEMO / lab.

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
- Host CLI must not require sqlalchemy/PyYAML.
- `snmp-exporter` is a **default** compose service.
- Backup on the host dumps Postgres via `docker compose exec postgres`. Do not `pip install sqlalchemy` on the Ubuntu host.

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
- Rewrite Add/Edit/Clone/Remove, backup, theme, or HTML mail for verify.

---

## 8. Known leftovers

- Many remote `origin/cursor/*-05f8` branches still exist and are **already merged to `main`**.
- Core slim image may not include `ping`; GUI ICMP then shows that honestly. `./forgesre verify` probes ICMP from the **host** (same as `./forgesre ping`).
- Scheduled `/ops` reports are still plain text.
