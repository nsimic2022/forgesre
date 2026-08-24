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
./forgesre ping
```

**Never** re-run `./install.sh` on a live box. That regenerates passwords in `secrets/secrets.env` and will wipe the install admin the operator already uses.

---

## 2. Why this session existed

Windows scrape (`windows_exporter` `:9182`) and `./forgesre ping` already shipped. N then asked ForgeSRE to **automatically determine Linux vs Windows** and listen on the matching exporter port:

- Linux: `node_exporter` **:9100**
- Windows: `windows_exporter` **:9182**

Default that choice in **Assets** and **Discovery**. Do not invent a fingerprinting framework.

---

## 3. What shipped on main

### 3.1 Auto OS / scrape port (this session)

Branch: `cursor/auto-os-scrape-port-05f8`.

Cheap HTTP detect from the ForgeSRE host (`backend/app/exporter_detect.py`):

1. GET `http://<ip>:9182/metrics` and `http://<ip>:9100/metrics` (short timeout).
2. Classify:
   - `:9182` body has `windows_exporter` / `windows_` → Windows Server, scrape `:9182`
   - `:9100` body has `node_exporter` / `node_` (`node_uname` / `node_cpu`) → Linux Server, scrape `:9100`
   - **Both:** keep a saved Linux/Windows type if set; else prefer Windows `:9182` (`windows_` vs `node_uname`/`node_cpu`). Documented in the module docstring.
   - **Neither:** type/port unset (`Unknown`). ICMP ping is not a scrape. Do not silently assume Linux.
3. ICMP is not used for OS.

Wired as the **default**:

- **Assets** add: type **Auto (detect exporter)** first. User can override. Edit page has **Detect OS / scrape port**.
- **Discovery:** when a host is alive, the same HTTP detect sets the proposed role / scrape. TCP 9100/9182 without exporter text is not an OS pick.
- **`./forgesre ping` / `probe`:** ad-hoc IPs already probed both ports; they now classify by the same `windows_` / `node_` family (not “HTTP 200 on :9100 wins”).

Explicit `Linux Server` / `Windows Server` still default `:9100` / `:9182` without a live probe (API and tests). Existing rows saved as Linux keep `:9100` until Detect or a type/scrape edit.

### 3.2 Already on main (do not redo)

- Windows Server asset type + HTTP SD `:9182` (`cursor/win-exporter-visibility-05f8`).
- `./forgesre ping` / `probe` (`cursor/cli-asset-ping-05f8`). Do not revert.

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
- Auto-detect is a helper + defaults, not a new fingerprinting subsystem or DB schema.

---

## 5. How to continue next session

1. `git pull origin main` (or fetch and check out `main`). Code lives there.
2. Read **this file**, then [`docs/llm.md`](llm.md) and [`docs/cli.md`](cli.md).
3. On the VM N uses: `git pull origin main && ./forgesre update`, then `./forgesre ping` and `./forgesre test`. Never `./install.sh` on that box. Hosts already saved as Linux Server with `:9100` need **Detect OS / scrape port** (or type Windows Server + `:9182`).
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
