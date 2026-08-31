# Session handoff — 31 August 2026

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

**Monday 31 August 2026.** Operator N (Serbian) asked for a smaller, significantly faster on-box LLM. The bundled default was Qwen2.5-14B-Instruct Q4_K_M (~9 GB), which cannot finish a CPU rewrite inside the **90s** timeout on the single worker.

On the Ubuntu VM N uses, resume with:

```bash
git pull origin main && ./forgesre update
```

Lab without image pull: `./forgesre update --offline`. Also: `./forgesre ping`, `./forgesre verify`, `./forgesre test` (appliance health, not inventory). See [docs/cli.md](cli.md) § Verify.

Hard-refresh the browser after UI/docs changes.

**Never** re-run `./install.sh` on a live box. That regenerates passwords in `secrets/secrets.env` and will wipe the install admin the operator already uses.

---

## 2. Checked twice (pytest)

From this branch, after `pip install -r requirements-dev.txt`:

```bash
PYTHONPATH=backend:agents python3 -m pytest tests
PYTHONPATH=backend:agents python3 -m pytest tests
```

Both runs: **333 passed**, 1 warning (Starlette `httpx` / `starlette.testclient` deprecation — ignore), ~40s each. Python 3.12, pytest 9.x.

If pytest fails next session: fix on a `cursor/<name>-05f8` branch, re-run **twice**, then `git merge --no-ff` to `main`. Branch pattern `cursor/<name>-05f8`.

---

## 3. Done today / on main

1. **Bundled LLM is now Qwen2.5-1.5B-Instruct Q4_K_M (~1.1 GB).** Official URL: `https://huggingface.co/Qwen/Qwen2.5-1.5B-Instruct-GGUF/resolve/main/qwen2.5-1.5b-instruct-q4_k_m.gguf`. Same ChatML family as the old 14B pin; no `--jinja`. Saved on disk as `$FORGESRE_DATA/models/model.gguf` (gitignored). ~2–4 GB extra RAM; 8 GB VM is enough with the stack.
2. **`./forgesre fetch-llm`** (existing CLI, not a new command) downloads that file, sets `COMPOSE_PROFILES=ai`, `ai.enabled: true` / `mode: bundled`, starts llama.cpp on `:8088`. Timeout stays **90s**. Compose context **4096**. `ai.enabled: false` remains the example.yml default.
3. **Docs = code.** [llm.md](llm.md), [cli.md](cli.md), [install-config.md](install-config.md), [operator-handbook.md](operator-handbook.md), CLI help, install wizard, admin cheatsheet. Optional Qwen3-4B wget path kept as a larger override.
4. **If `data/models/model.gguf` is already a 14B/4B file, fetch-llm skips.** Delete it, then `./forgesre fetch-llm`. Live `config/forgesre.yml` is gitignored — `model: local` is still correct (compose always loads `model.gguf`). A leftover `timeout_seconds: 600` in that file is not overwritten by `update`; lower it to 90 by hand.

Runtime stays llama.cpp. Do **not** switch the default to Ollama.

---

## 4. What N should do on the VM

Do **not** run `./install.sh`.

```bash
git pull origin main && ./forgesre update
```

If LLM is already on and `data/models/model.gguf` is still the old 14B (~9 GB) or 4B file:

```bash
rm -f data/models/model.gguf
./forgesre fetch-llm
```

If LLM was never enabled:

```bash
./forgesre fetch-llm
```

Wait until `:8088` answers (`curl -fsS http://127.0.0.1:8088/v1/models`), then `./forgesre doctor` and `./forgesre test`. Hard-refresh the browser. ForgeRCA still runs without a GGUF.

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
- Bundled **NetBox** is a **default** compose service (`:8001`). Do not put it behind a profile. `--netbox-url` remains an external override. Image pin is `netboxcommunity/netbox:v4.6.9-5.0.2` (not `v4.4-3.2.0`). Do not churn NetBox Hub/GHCR tags unless N asks.
- Dashboard **HOST DOWN** banner (open exporter/SNMP-down incidents). Do not redo it.
- Backup on the host dumps Postgres via `docker compose exec postgres` with the same docker rights as `./forgesre update` (`docker info`, else `sudo docker compose`). A docker.sock permission error is not “start postgres”.
- One restore unit = one `.tar.gz` inside `backup_<stamp>/`. Do not explode the archive into loose files at `data/backups/` root.
- Host `./forgesre verify` does not import sqlalchemy (`demo_ids.py`).
- Memory bundled alerts exist: `NodeMemoryHigh` / `WindowsMemoryHigh` at **90%**, playrules `node-memory` / `windows-memory`. Grafana is not the alarm path.
- Add asset: operator types **Asset ID** and **Hostname** separately. Id is immutable after create. Do not derive id from hostname again.
- Doctor labels: **Core API** (`doctor.sh` health curl) vs **Core (container)** (payload `/api/v1/health` probe). Do not rename them back to a single “Core”.
- The lab SMTP catcher is gone. Do not add one.
- Verify hops: ICMP, PORT, FAMILY, PROM, TARGET, SERIES, AM, CORE, RCA, LLM. LLM SKIP: verify does not invoke the LLM. Reachability: ping **green** ICMP ok; **yellow** ICMP fail but exporter/SNMP ok; **red** both fail. ICMP is not TCP 22.
- Linux metrics = node_exporter **:9100**. Windows = windows_exporter **:9182**. Network = snmp_exporter :9116 (Prom still scrapes it). Network is not guessed from missing HTTP.
- Bundled LLM = **Qwen2.5-1.5B-Instruct Q4_K_M** via `./forgesre fetch-llm`. llama.cpp on `:8088`. Timeout **90s**. Do not raise it back to 600s. Do not switch the default runtime to Ollama.

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

---

## 8. Known leftovers

- Many remote `origin/cursor/*-05f8` branches still exist and are **already merged to `main`**.
- Core slim image may not include `ping`; GUI ICMP then shows that honestly. `./forgesre verify` probes ICMP from the **host** (same as `./forgesre ping`).
- Scheduled `/ops` reports are still plain text.
- Old backups already on the VM as `data/backups/forgesre-*.tar.gz` are still valid; new runs write folders.
- Grafana deep-link from an asset is still later.
- Prometheus global rules may still fire for a host whose ForgeSRE alarm is disabled or raised; ForgeSRE will not open the incident when the webhook carries the value.
- Alloy still only ships appliance Core logs as `forge-demo-01`. Real hosts have no Loki until that changes. Limitation: **no host logs shipped**.
- LLM rewrite can still occupy the single job loop for up to `timeout_seconds` (default 90) after reports in that pass have already run.
- `config/forgesre.yml` on a live VM is gitignored; a 600s timeout already written there is not overwritten by `update`. Lower it by hand if the worker still blocks.
- An existing `data/models/model.gguf` larger than 400 MB is not replaced by `fetch-llm`. Delete the old 14B/4B file to pull 1.5B.
