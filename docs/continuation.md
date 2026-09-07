# Session handoff — 7 September 2026

This file is a **session handoff for the next coding agent or contributor**. It is not an operator manual. Operators start at [install and config](install-config.md) and the [operator handbook](operator-handbook.md).

Product on `main` at the end of this session: **V0.7**. Repository: https://github.com/nsimic2022/forgesre.

1. [Who and when](#1-who-and-when)
2. [Checked twice (pytest)](#2-checked-twice-pytest)
3. [Done today / on this branch](#3-done-today--on-this-branch)
4. [What N should do on the VM](#4-what-n-should-do-on-the-vm)
5. [Product facts not to redo](#5-product-facts-not-to-redo)
6. [How to continue next session](#6-how-to-continue-next-session)
7. [Out of scope](#7-out-of-scope)
8. [Known leftovers](#8-known-leftovers)

---

## 1. Who and when

**Monday 7 September 2026.** Operator N (Serbian) asked for the ordered GUI/platform/docs list (dashboard doctor demotion through Core `iputils-ping`). Code and docs stay English. Replies to N are Serbian.

On the Ubuntu VM N uses:

```bash
git pull origin main && ./forgesre update
```

Then **hard-refresh** the browser (`Ctrl+Shift+R`) so `/static/app.css?v=n-ordered-1` is not a cached old sheet.

**Never** re-run `./install.sh` on a live box. That regenerates passwords in `secrets/secrets.env` and will wipe the install admin the operator already uses.

---

## 2. Checked twice (pytest)

From this branch, after `pip install -r requirements-dev.txt`:

```bash
PYTHONPATH=backend:agents python3 -m pytest tests
PYTHONPATH=backend:agents python3 -m pytest tests
```

Both runs: **339 passed**, 2 warnings (Starlette `httpx` / `starlette.testclient` deprecation — ignore), 41.34s then 41.04s. Python 3.12, pytest 9.x. Count recorded after the double run on `cursor/n-ordered-improvements-05f8` (was 334 before these GUI/docs tests).

If pytest fails next session: fix on a `cursor/<name>-05f8` branch, re-run **twice**, then `git merge --no-ff` to `main`. Branch pattern `cursor/<name>-05f8`.

---

## 3. Done today / on this branch

N’s ordered list, in order:

1. **Dashboard** — no duplicate doctor grid. HOST DOWN banner, stats, recent incidents stay. Link **System Health**.
2. **Incident page** — short RCA preview; one primary CTA **Open ForgeRCA** → `/ai/{number}`. Ack/Resolve/Who to call stay. ForgeRCA/ForgeAI pills stay.
3. **Mail** — one outbox table on `/ops#mail`. Incident and Escalation **link** there. **Send incident report** stays on the incident.
4. **Asset detail** — one **Edit Alarms** on the metrics panel, not Edit on every tile.
5. **Incidents vs History** — open/firing vs archive copy. `/incidents` filter: open-only / last days.
6. **Discovery** — demo `10.20.30.41` has a DEMO pill and lab-seed copy. **Scan now** separate from **Sync NetBox**. NetBox read-only.
7. **Assets** — one sentence: verify ≠ doctor ≠ `./forgesre test`.
8. **`POST /demo*`** (HTML) uses `require_page("admin")`, not mere `login_required`. API already required admin.
9. **Grafana** stays out of left nav and out of the alarm path. Open only from `/health-ui`. Alarm path: Prom → AM → Core.
10. Discovery stays TCP/SNMP/HTTP probe — **not nmap**. Copy says so. NetBox read-only.
11. **No new log stack.** Alloy still only ships appliance/Core logs as demo. Docs + RCA one-liner when host logs are empty.
12. **No Celery.** Docs: one worker thread; LLM rewrite can occupy it up to `timeout_seconds`. Health/AI copy.
13. UI/docs: `alerts.yml` = PromQL source; asset Alarms = per-host enable/% overlay, not a second engine.
14. Playrules: shorter copy — map `alertname` → playbook, do not create Prom rules.
15. `docs/architecture.md` banner: **architecture proposal / not the V0.7 appliance runtime** (Compose, bundled NetBox, Redis for NetBox, no Caddy-as-runtime). Do not implement the Go/K8s rewrite. Operators: handbook + CLI.
16. Core Dockerfile installs **`iputils-ping`**. GUI ICMP is from the Core container; host `./forgesre ping` stays on the VM. Do not `pip install sqlalchemy` on the host.

LLM prompt shrink (5000 chars) and reverted catalog stay. Did not add Celery, nmap, or a second log stack.

---

## 4. What N should do on the VM

Do **not** run `./install.sh`.

```bash
git pull origin main && ./forgesre update
```

`./forgesre update` rebuilds Core (Dockerfile now has `iputils-ping`; frontend CSS cache-bust `n-ordered-1`). Then hard-refresh the browser.

Lab without image pull: `./forgesre update --offline`.

---

## 5. Product facts not to redo

These already work on `main`. Do not “fix” them unless N asks.

- `./forgesre test` = appliance health report → `data/reports/`. `./forgesre verify` = live inventory communication. `./forgesre doctor` = System Health lights. Three different commands.
- Theme toggle cycles **light → dark → system**. Left nav stays dark.
- Dashboard demos are **one** top-right button + a closeable panel. Demo rows stay **labeled DEMO**. Demo inject is **admin**.
- Incident ids look like `INC-0134_16.08.2026_09:13`.
- RCA is Python under `agents/rca/`. The LLM only rewrites prose. Builtin ForgeRCA always runs first. LLM payload is compact (5000 chars); stored investigation is full.
- Core is an SMTP **client** only. The UI has no IMAP inbox. One mail outbox: `/ops#mail`.
- pytest is a laptop/dev dependency. The Core image must not install it.
- Real Windows scrape is **windows_exporter :9182**, not the lab demo host.
- Prometheus Health Open is **Targets** (`:9090/targets?search=`), not Prometheus process `/metrics`. Core `/metrics` stays.
- Host CLI must not require sqlalchemy/PyYAML. Do not `pip install sqlalchemy` on the Ubuntu host.
- `snmp-exporter` is a **default** compose service.
- Bundled **NetBox** is a **default** compose service (`:8001`). Do not put it behind a profile. `--netbox-url` remains an external override. Image pin is `netboxcommunity/netbox:v4.6.9-5.0.2`. Do not churn NetBox Hub/GHCR tags unless N asks.
- Dashboard **HOST DOWN** banner (open exporter/SNMP-down incidents). Do not redo it.
- Backup on the host dumps Postgres via `docker compose exec postgres` with the same docker rights as `./forgesre update`.
- One restore unit = one `.tar.gz` inside `backup_<stamp>/`.
- Host `./forgesre verify` does not import sqlalchemy (`demo_ids.py`).
- Memory bundled alerts exist: `NodeMemoryHigh` / `WindowsMemoryHigh` at **90%**, playrules `node-memory` / `windows-memory`. Grafana is not the alarm path.
- Add asset: operator types **Asset ID** and **Hostname** separately. Id is immutable after create.
- Doctor labels: **Core API** vs **Core (container)**.
- The lab SMTP catcher is gone. Do not add one.
- Verify hops: ICMP, PORT, FAMILY, PROM, TARGET, SERIES, AM, CORE, RCA, LLM. Reachability: ping **green** ICMP ok; **yellow** ICMP fail but exporter/SNMP ok; **red** both fail.
- Linux metrics = node_exporter **:9100**. Windows = windows_exporter **:9182**. Network = snmp_exporter :9116.
- Bundled LLM pin = **Qwen2.5-14B-Instruct Q4_K_M** via `./forgesre fetch-llm`. Do **not** restore the GGUF catalog / Health picker.
- `docs/architecture.md` is a **proposal**, not the appliance runtime.

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
- NetBox Docker Hub / GHCR tag churn (separate).
- Rewriting discovery as nmap.
- Making Ollama the product default (N will decide later).
- Re-pinning bundled `fetch-llm` to Qwen2.5-1.5B or another GGUF.
- Restoring the llama.cpp GGUF catalog / Health model switcher.
- Raising `timeout_seconds` in `config/forgesre.example.yml` back to 600.

---

## 8. Known leftovers

- Many remote `origin/cursor/*-05f8` branches still exist and are **already merged to `main`**.
- Scheduled `/ops` reports are still plain text.
- Old backups already on the VM as `data/backups/forgesre-*.tar.gz` are still valid; new runs write folders.
- Grafana deep-link from an asset is still later.
- Prometheus global rules may still fire for a host whose ForgeSRE alarm is disabled or raised; ForgeSRE will not open the incident when the webhook carries the value.
- Alloy still only ships appliance Core logs as `forge-demo-01`. Real hosts have no Loki until that changes. Limitation: **no host logs shipped**.
- LLM rewrite can still occupy the single job loop for up to `timeout_seconds` (default 90 in example.yml; N’s live yml may be 300) after reports in that pass have already run.
- `config/forgesre.yml` on a live VM is gitignored; a 300s or 600s timeout already written there is not overwritten by `update`.
