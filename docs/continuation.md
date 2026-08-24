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

Earlier the same week (21 August) shipped appliance test, CLI quit help, and the lab Qwen3-4B wget path. Those remain on `main`. This file now describes the 24 August UI/ops polish session.

---

## 2. Why this session existed

N asked to apply the agreed ops/UI polish from chat, **except ticketing**. Do not add Jira, TheHive, IMAP, or any ticketing system. Ticketing ideas were explained to N in chat only.

Agreed work: collect Alertmanager evidence once per firing burst, keep pytest out of the Core image, small backend tidy, and dark-ops-console CSS/nav polish. No redesign, no React/Tailwind/Bootstrap, no light theme, no mailbox-on, no Go/K8s, no `./install.sh` as the update path.

---

## 3. What shipped on main

Branch: `cursor/ui-ops-polish-05f8`.

### 3.1 Webhook evidence once

`ingest_alertmanager` still creates the incident and **enqueues** `investigate` with `use_llm=False` (LLM rewrite is queued after the builtin job). It does **not** run full RCA inline.

Prometheus/Loki evidence is collected **once** for a new incident (or if the incident has no evidence yet). Repeat firing webhooks for the same open incident do not collect again. `run_investigation` reuses stored evidence unless `force=True`. `persist_rca_evidence` builds RCA items from that snapshot and does not query Prom/Loki a second time.

`./forgesre demo` still opens HighCPU and still runs builtin RCA immediately on the demo path so the first-hour walkthrough is visible.

Tests: `tests/test_hardening.py` (`test_webhook_enqueues_investigation_job`, `test_webhook_evidence_collected_once`).

### 3.2 pytest out of the Core image

- `backend/requirements.txt` — runtime only. **No pytest.**
- `requirements-dev.txt` — `-r backend/requirements.txt` plus pytest.
- `backend/Dockerfile` still `pip install -r /app/requirements.txt` only.

Laptop:

```bash
pip install -r requirements-dev.txt
PYTHONPATH=backend:agents pytest tests
```

### 3.3 Small backend tidy

- `/api/v1/system/status` uses SQL `COUNT` / `GROUP BY` instead of loading every asset and incident.
- Shared `utcnow` lives in `app.models` and is imported from there (services, jobs, journal, seed, inventory, history).
- FastAPI `lifespan` replaced deprecated `on_event` in `backend/app/main.py`. Startup order is unchanged: `create_all` → `migrate` → `seed` → demo candidate → journal reports → background threads. Shutdown still `stop.set()`.

### 3.4 Docs

- Removed the false `FOR UPDATE SKIP LOCKED` claim. `backend/app/jobs.py` still claims work with `.filter_by(status="pending").order_by(Job.id).first()`.
- [`docs/architecture.md`](architecture.md) ADR-4 and this file match that code.
- [`CONTRIBUTING.md`](../CONTRIBUTING.md) and [`docs/verify.md`](verify.md) use `requirements-dev.txt`.

### 3.5 UI (same dark ops console)

No new pages, no nav restructure, no new color system.

- `:focus-visible` uses existing `--accent`.
- Nav `active` on Playrules, Playbooks, Escalation, Administration (`base.html` `startswith`, same as Assets/Incidents).
- True/False → On/Off on playrules, scheduled reports, discovery enabled.
- Logout is `class="secondary"`.
- Incident detail RCA/investigation block has `id="ai"` (alongside `#mail`, `#audit`, `#notes`).
- Wide tables (assets, history, admin) sit in `.table-scroll` (`overflow-x: auto`). `.content` also scrolls if a table is wider than the pane.
- `.pill.offline` is muted like ignored. Escalation outbox status uses `generated` / `sent` / `failed` pills.
- CSS only: type/spacing, button padding, table row hover, slightly larger card radius. Tokens unchanged.

`incident_detail.html` and `ai.html` still both show RCA. They are not the same page (ops summary vs full engineer investigation with limitations and evidence chain), so they were **not** forced into one Jinja partial.

After UI/CSS changes, operators need a **hard refresh** (`/static/app.css` has no cache-busting query).

---

## 4. Product facts not to redo

These already work on `main`. Do not “fix” them unless N asks.

- Incident ids look like `INC-0134_16.08.2026_09:13` (sequence + local date/time). Older `INC-000012` rows stay valid. TAB completes those ids after `incidents` / `history`.
- RCA is Python under `agents/rca/`. The LLM only rewrites prose. Builtin ForgeRCA always runs first. Alertmanager ingest enqueues investigate; demo still runs builtin RCA immediately.
- Secrets are gitignored (`secrets/`, `.env`, `config/forgesre.yml`, `data/`). Background jobs live in **Postgres** (`jobs` table, pending/running/done). Claim is not `SKIP LOCKED`.
- Core is an SMTP **client** only. The UI has no IMAP inbox. Humans read replies in Gmail, Outlook, or (later) Roundcube.
- Gmail and Outlook / Microsoft 365 are the supported send path now. Compose profile `mailbox` stays **off** until `./forgesre mailbox`. That command must **not** rewrite Core SMTP unless `--bind-core`.
- UI users are bcrypt hashes in Postgres. Administration is click-to-edit. You cannot delete yourself or the install `super_admin`.
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
- Do **not** add React, Tailwind, Bootstrap, a new icon pack, or a light theme.

---

## 7. Optional leftovers

These are documentation choices, not holes to fill on sight:

- `curl /health` is documented in [`docs/llm.md`](llm.md). It is not duplicated in every Advanced CLI list (`docs/install-config.md` §13 points at llm.md §8).
- The GitHub README `fetch-llm` line does not inline the 8 GB wget. The lab wget lives in [`docs/llm.md`](llm.md) §3.C.
- Job claim could later use `FOR UPDATE SKIP LOCKED` on Postgres. Do not pretend it already does. SQLite tests would need a fallback.
- `incident_detail.html` / `ai.html` RCA markup is similar but not identical. Extract a partial only if both pages should look the same.
