# Session handoff — 24 August 2026

This file is a **session handoff for the next coding agent or contributor**. It is not an operator manual. Operators start at [install and config](install-config.md) and the [operator handbook](operator-handbook.md).

Product on `main` at the end of this session: **V0.7**. Repository: https://github.com/nsimic2022/forgesre.

1. [Who and when](#1-who-and-when)
2. [Why this session existed](#2-why-this-session-existed)
3. [What shipped on main](#3-what-shipped-on-main)
4. [Product facts not to redo](#4-product-facts-not-to-redo)
5. [How to continue next session](#5-how-to-continue-next-session)
6. [Out of scope](#6-out-of-scope)
7. [Optional leftovers](#7-optional-leftovers)

---

## 1. Who and when

**Monday 24 August 2026.** Operator N ran a cloud-agent coding session against this repository. All of the work described below is merged to **`main`**. Do not treat unmerged feature branches as shipped.

On the Ubuntu VM N uses, resume with:

```bash
git pull origin main
./forgesre update
```

Hard-refresh the browser after UI/CSS changes.

**Never** re-run `./install.sh` on a live box. That regenerates passwords in `secrets/secrets.env` and will wipe the install admin the operator already uses.

---

## 2. Why this session existed

Assets had **Add asset** but no **Edit / Clone / Remove**. Those are mandatory. N also asked to review Add fields (only keep what inventory, Discovery, Prometheus scrape, and contacts actually use).

---

## 3. What shipped on main

### 3.1 Asset Edit / Clone / Remove (this session)

Branch: `cursor/assets-edit-clone-remove-05f8`.

- List page Actions: **Edit**, **Clone**, **Remove** (same `write_assets` as Add: analyst / engineer / admin). Viewers do not see them.
- **Edit** reuses the Add form (`/assets?edit=<id>`). Hostname, type including Auto, IP, scrape address, owner/contact, notes, environment. `asset_id` is stable. HTTP SD is live from Postgres; Prometheus picks changes on the next scrape. Core static demo scrape is untouched.
- **Clone** prefills the same form with a new hostname/id. User tweaks before Save. Duplicate hostname/IP rejected. Cloning `forge-demo-*` suggests `copy-…` (real asset, can be scraped) unless they keep a `forge-demo-*` name.
- **Remove** confirms. Deletes the inventory row (drops HTTP/SNMP SD). **Keeps incidents** (clears `incidents.asset_id`). Discovery candidates for that IP return to `new`. **Blocks** seeded `forge-demo-*` (demos / similar history).
- Add form gained optional **scrape address** (exporter port). No invented ticketing / Zabbix / IMAP fields.

API: `POST /api/v1/assets/{id}`, `/clone`, `/delete`.

### 3.2 Already on main (do not redo)

- Windows Server + windows_exporter `:9182` vs Linux node_exporter `:9100`.
- Auto-detect OS/port in Assets/Discovery (`exporter_detect.py`, default Auto).
- `./forgesre ping` / `probe`.
- Administration user edit/remove (click user). Do not fight a sibling Administration backup/import change.

---

## 4. Product facts not to redo

These already work on `main`. Do not “fix” them unless N asks.

- Theme toggle cycles **light → dark → system**. Default is **light**. The left nav is a **constant dark shell**; only the main pane changes. **System** follows OS `prefers-color-scheme` for the content area. Persist with `forgesre-theme`. Stored `high-contrast` is treated as **dark**. `/static/app.css` is not cache-busted — hard-refresh after CSS changes.
- Dashboard demos are **one** top-right button + a closeable panel. Do not put two always-visible demo forms back in the monitoring column.
- Demo rows stay visible; they are **labeled DEMO**, not hidden. `./forgesre ping` skips `forge-demo-*` unless `--demo` or the id is passed.
- Incident ids look like `INC-0134_16.08.2026_09:13`. Older `INC-000012` rows stay valid.
- RCA is Python under `agents/rca/`. The LLM only rewrites prose. Builtin ForgeRCA always runs first.
- Core is an SMTP **client** only. The UI has no IMAP inbox. Incident reports and escalation mail are HTML + plain text (multipart); `/ops` compose stays plain.
- pytest is a laptop/dev dependency. The Core image must not install it.
- After UI / CSS changes, operators need a **hard refresh** in the browser.
- Real Windows scrape is **windows_exporter :9182**, not the lab demo host.
- ICMP ping ≠ Prometheus scrape. Do not add a ping-only “host up” incident.
- Auto-detect is a helper + defaults, not a new fingerprinting subsystem. Network is only the existing SNMP UDP/161 path (`snmp_ok` / live GET). Do not guess Network from missing :9100/:9182.
- Assets Ping / :9100 / :9182 / SNMP dots are last-known + async (`GET /api/v1/assets/reachability`). Do not probe inside the HTML list handler.

---

## 5. How to continue next session

1. `git pull origin main` (or fetch and check out `main`). Code lives there.
2. Read **this file**, then [`docs/llm.md`](llm.md) and [`docs/cli.md`](cli.md).
3. On the VM N uses: `git pull origin main && ./forgesre update`, then `./forgesre ping` and `./forgesre test`. Hard-refresh Assets. Never `./install.sh` on that box.
4. Developer checks: `pip install -r requirements-dev.txt` if needed, then `PYTHONPATH=backend:agents pytest tests` **twice**, then merge to `main`. New work uses branch pattern `cursor/<name>-05f8`.
5. Replies to N are in **Serbian**. OSS docs and code stay in **English**.
6. `ManagePullRequest` `create_pr` / `update_pr` often 403. `git merge --no-ff` plus `git push origin main` still lands the change.

---

## 6. Out of scope

Do not start these unless N asks:

- Do **not** implement a ticketing system (Jira, TheHive, IMAP inbox, or similar).
- Do **not** implement the longer-term Go / Kubernetes rewrite described in [`docs/architecture.md`](architecture.md). That document is a design note, not a sprint.
- Do **not** change the default `./forgesre fetch-llm` URL from Qwen2.5-14B-Instruct Q4_K_M to 4B unless N asks.
- Do **not** enable Compose profile `mailbox` by default.
- Do **not** put a GGUF (or any model weights) in git.
- Do **not** add React, Tailwind, Bootstrap, PatternFly npm, or a new icon pack. OS theme is **system** mode for the content pane only; the sidebar stays a constant dark shell.
- Do **not** fake a live Windows scrape or SNMP walk in the demo panel.

---

## 7. Optional leftovers

These are documentation choices, not holes to fill on sight:

- `curl /health` is documented in [`docs/llm.md`](llm.md). It is not duplicated in every Advanced CLI list (`docs/install-config.md` §13 points at llm.md §8).
- The GitHub README `fetch-llm` line does not inline the 8 GB wget. The lab wget lives in [`docs/llm.md`](llm.md) §3.C.
- Job claim could later use `FOR UPDATE SKIP LOCKED` on Postgres. Do not pretend it already does. SQLite tests would need a fallback.
- `incident_detail.html` / `ai.html` RCA markup is similar but not identical. Extract a partial only if both pages should look the same.
- Scheduled performance reports on `/ops` are still plain text. HTML them only if N asks.
- Existing Windows hosts added as `Linux Server` keep `:9100` until Detect or a type/scrape edit. No automatic rewrite of custom scrape addresses on every save.
