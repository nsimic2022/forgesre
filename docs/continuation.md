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

**Never** re-run `./install.sh` on a live box. That regenerates passwords in `secrets/secrets.env` and will wipe the install admin the operator already uses.

---

## 2. Why this session existed

N asked for **at least five Run demo scenarios** covering Linux, Windows, and network, with **DEMO** visible on incidents, history, escalation, email, and journal/logs. Keep the existing three Linux cards. Do not claim windows_exporter scraping or a live SNMP walk. No Zabbix, no ticketing.

---

## 3. What shipped on main

Branch: `cursor/demo-scenarios-05f8`.

### 3.1 Six Run demo scenarios

Same one top-right **Run demo** button and closeable panel. Cards:

1. **Linux HighCPU** — `forge-demo-01` (existing).
2. **Linux disk / ForgeRCA** — `forge-demo-01` (existing).
3. **Linux host unreachable** — `forge-demo-01` (existing).
4. **Windows CPU (lab)** — `WindowsCPUHigh` on seeded `forge-demo-win-01`. Copy says lab / DEMO; does **not** claim windows_exporter is scraping.
5. **Network SNMP unreachable (lab)** — `SnmpDeviceUnreachable` on seeded `forge-demo-sw-01`. Copy says lab / DEMO; does **not** claim a live SNMP walk. The switch is excluded from snmp HTTP SD.
6. **Linux NodeCPUHigh** — extra Linux alertname on `forge-demo-01`.

**Reset demo gauges** stays in that panel. CLI `./forgesre demo`, `demo-rca`, and `demo-reset` are unchanged.

### 3.2 DEMO marking

`is_demo_asset_id()` is true for any `forge-demo-*` id. `is_demo_incident()` / CLI `item_is_demo()` / mail `[DEMO]` + body line / journal summaries all use that helper. UI pills on incidents, history, detail, ForgeRCA, escalation, email outbox, dashboard journal, and `/journal`. Seeded hosts: `forge-demo-01`, `forge-demo-win-01`, `forge-demo-sw-01`. Dashboard yellow journal error banner has **Dismiss**; per-user `users.journal_error_ack_id` hides it until a newer error.

---

## 4. Product facts not to redo

These already work on `main`. Do not “fix” them unless N asks.

- Theme is **manual**. Default is **light**. It does not follow the OS. Persist with `forgesre-theme`.
- Dashboard demos are **one** top-right button + a closeable panel. Do not put two always-visible demo forms back in the monitoring column.
- Demo rows stay visible; they are **labeled DEMO**, not hidden.
- Incident ids look like `INC-0134_16.08.2026_09:13`. Older `INC-000012` rows stay valid.
- RCA is Python under `agents/rca/`. The LLM only rewrites prose. Builtin ForgeRCA always runs first.
- Core is an SMTP **client** only. The UI has no IMAP inbox.
- pytest is a laptop/dev dependency. The Core image must not install it.
- After UI / CSS changes, operators need a **hard refresh** in the browser.

---

## 5. How to continue next session

1. `git pull origin main` (or fetch and check out `main`). Code lives there.
2. Read **this file**, then [`docs/llm.md`](llm.md) and [`docs/cli.md`](cli.md).
3. On the VM N uses: `git pull origin main && ./forgesre update`, then `./forgesre test`. Never `./install.sh` on that box.
4. Developer checks: `pip install -r requirements-dev.txt` if needed, then `PYTHONPATH=backend:agents pytest tests` **twice**, then merge to `main`. New work uses branch pattern `cursor/<name>-05f8`.
5. Replies to N are in **Serbian**. OSS docs and code stay in **English**.
6. `ManagePullRequest` `update_pr` often fails with “PR URL must belong to the current repository”. `git merge` plus `git push origin main` still lands the change. Prefer that over fighting the PR updater.

---

## 6. Out of scope

Do not start these unless N asks:

- Do **not** implement a ticketing system (Jira, TheHive, IMAP inbox, or similar).
- Do **not** implement the longer-term Go / Kubernetes rewrite described in [`docs/architecture.md`](architecture.md). That document is a design note, not a sprint.
- Do **not** change the default `./forgesre fetch-llm` URL from Qwen2.5-14B-Instruct Q4_K_M to 4B unless N asks.
- Do **not** enable Compose profile `mailbox` by default.
- Do **not** put a GGUF (or any model weights) in git.
- Do **not** add React, Tailwind, Bootstrap, or a new icon pack. Do **not** follow the OS theme.
- Do **not** fake a live Windows scrape or SNMP walk in the demo panel.

---

## 7. Optional leftovers

These are documentation choices, not holes to fill on sight:

- `curl /health` is documented in [`docs/llm.md`](llm.md). It is not duplicated in every Advanced CLI list (`docs/install-config.md` §13 points at llm.md §8).
- The GitHub README `fetch-llm` line does not inline the 8 GB wget. The lab wget lives in [`docs/llm.md`](llm.md) §3.C.
- Job claim could later use `FOR UPDATE SKIP LOCKED` on Postgres. Do not pretend it already does. SQLite tests would need a fallback.
- `incident_detail.html` / `ai.html` RCA markup is similar but not identical. Extract a partial only if both pages should look the same.
