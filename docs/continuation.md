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

HTML incident mail already shipped. N then tried to add a **Windows** host with an exporter and ForgeSRE did not see it. They already have **ICMP ping** from the Linux appliance to that machine and asked whether ping / VM availability should be built into the ForgeSRE CLI.

ICMP ping only proves L3. ForgeSRE "seeing" the host needs Prometheus to scrape **windows_exporter `:9182/metrics`** (not Linux node_exporter `:9100`).

---

## 3. What shipped on main

Two branches, same day:

### 3.1 Real Windows scrape (already on main)

Branch: `cursor/win-exporter-visibility-05f8`.

- Add Asset type **Windows Server** → `monitoring_profile=windows-standard`, `scrape_address=<ip>:9182`.
- Same Prometheus HTTP SD (`/api/v1/sd/prometheus`) as Linux. Label `job=windows-standard`.
- Bundled alerts: `WindowsExporterDown`, `WindowsFilesystemUsageHigh`, `WindowsCPUHigh`.
- Discovery probes TCP **9182**. Seeded `forge-demo-*` stay out of HTTP SD.

Linux `node_exporter` `:9100` is unchanged. Do not revert this wiring.

### 3.2 `./forgesre ping` / `./forgesre probe`

Branch: `cursor/cli-asset-ping-05f8`.

Host CLI command. Default: every non-demo inventory row that has an IP or `scrape_address`. Optional selector: asset id, hostname, or IP.

Each row prints:

- **ICMP** PASS/FAIL (one `ping -c 1` from the appliance)
- **METRICS** PASS/FAIL — HTTP GET `/metrics` on the configured scrape port, or Linux `:9100` / Windows `:9182` from asset type
- timeout (default 2s), overall PASS/FAIL, exit 1 on FAIL

ICMP PASS + METRICS FAIL means the VM is up but ForgeSRE cannot scrape it (exporter down, firewall on 9182/9100, or wrong port). Network devices SKIP HTTP and point at `./forgesre snmp`.

Code: `backend/app/asset_probe.py`, `cli_ops.py` (`ping`/`probe`). Help: `./forgesre help ping`. Docs: [`cli.md`](cli.md), operator handbook §7 / §14.

On the VM: `git pull origin main && ./forgesre update`, then `./forgesre ping`. Recreate Core if the backend image does not pick up the new Python modules.

---

## 4. Product facts not to redo

These already work on `main`. Do not “fix” them unless N asks.

- Theme is **manual**. Default is **light**. It does not follow the OS. Persist with `forgesre-theme`.
- Dashboard demos are **one** top-right button + a closeable panel. Do not put two always-visible demo forms back in the monitoring column.
- Demo rows stay visible; they are **labeled DEMO**, not hidden. `./forgesre ping` skips `forge-demo-*` unless `--demo` or the id is passed.
- Incident ids look like `INC-0134_16.08.2026_09:13`. Older `INC-000012` rows stay valid.
- RCA is Python under `agents/rca/`. The LLM only rewrites prose. Builtin ForgeRCA always runs first.
- Core is an SMTP **client** only. The UI has no IMAP inbox. Incident reports and escalation mail are HTML + plain text (multipart); `/ops` compose stays plain.
- pytest is a laptop/dev dependency. The Core image must not install it.
- After UI / CSS changes, operators need a **hard refresh** in the browser.
- Real Windows scrape is **windows_exporter :9182**, not the lab demo host.
- ICMP ping ≠ Prometheus scrape. Do not add a ping-only “host up” incident.

---

## 5. How to continue next session

1. `git pull origin main` (or fetch and check out `main`). Code lives there.
2. Read **this file**, then [`docs/llm.md`](llm.md) and [`docs/cli.md`](cli.md).
3. On the VM N uses: `git pull origin main && ./forgesre update`, then `./forgesre ping` and `./forgesre test`. Never `./install.sh` on that box.
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
- Scheduled performance reports on `/ops` are still plain text. HTML them only if N asks.
- Existing Windows hosts added as `Linux Server` keep `:9100` until the operator edits type/scrape. No automatic rewrite of custom scrape addresses. `./forgesre ping` reports that mismatch.
