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

**Monday 31 August 2026.** Operator N (Serbian) asked for **Ollama** and said **do not change code yet** — they will decide later whether any product change is needed. Another agent had already merged a llama.cpp GGUF swap to `main` (`cc6a9990636a29a0a7c203b5c6e676c09fa5b234`, branch `cursor/small-fast-llm-05f8`, merge `--no-ff` because `create_pr` 403): Qwen2.5-14B-Instruct Q4_K_M (~9 GB) → Qwen2.5-1.5B-Instruct Q4_K_M (~1.1 GB), compose context 4096. N did not approve that code change. This session **reverts only that GGUF pin**. NetBox tags and verify `:9100` work stay on `main`.

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

Both runs: **332 passed**, 1 warning (Starlette `httpx` / `starlette.testclient` deprecation — ignore), ~40s each. Python 3.12, pytest 9.x. (Count recorded after the double run on this revert branch.)

If pytest fails next session: fix on a `cursor/<name>-05f8` branch, re-run **twice**, then `git merge --no-ff` to `main`. Branch pattern `cursor/<name>-05f8`.

---

## 3. Done today / on main

1. **Reverted merge `cc6a999` (`git revert -m 1`).** Bundled GGUF pin restored to **Qwen2.5-14B-Instruct Q4_K_M** (~9 GB). Default URL is again `https://huggingface.co/Qwen/Qwen2.5-14B-Instruct-GGUF/resolve/main/qwen2.5-14b-instruct-q4_k_m.gguf`. Compose llama.cpp context is **`-c 8192`** again. Docs, `fetch-llm`, example.yml, install wizard, and appliance tests match the restored pin.
2. **Ollama is not a product default.** It remains an operator YAML choice: `ai.enabled: true`, `ai.llm.mode: external`, `url: http://127.0.0.1:11434/v1` (and a real Ollama model id). No compose/Ollama service, no default-url change. N can still point live `config/forgesre.yml` at Ollama without this repo switching defaults.
3. **Did not touch** NetBox image tags (`netboxcommunity/netbox:v4.6.9-5.0.2`) or the verify default-port work (`:9100` / `:9182`).
4. **Did not run `./install.sh`.** Did not implement Ollama as default. The 1.5B GGUF swap stays undone until N asks.

---

## 4. What N should do on the VM

Do **not** run `./install.sh`.

```bash
git pull origin main && ./forgesre update
```

The 1.5B GGUF swap is undone. Bundled `./forgesre fetch-llm` is 14B again. If N wants **Ollama** instead of the bundled llama.cpp GGUF, leave `COMPOSE_PROFILES` without `ai` and set live YAML (gitignored) to:

```yaml
ai:
  enabled: true
  llm:
    mode: external
    url: http://127.0.0.1:11434/v1
    model: <ollama-model-id>
    timeout_seconds: 90
```

Then recreate Core (`docker compose up -d --force-recreate core`) and `./forgesre doctor`. That is an operator config, not a product default. Hard-refresh the browser.

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
- Linux metrics = node_exporter **:9100**. Windows = windows_exporter **:9182**. Network = snmp_exporter :9116 (Prom still scrapes it). Network is not guessed from missing HTTP. Verify with empty `scrape_address` probes those default exporter ports (already on `main` from `e95280a`).
- Bundled LLM pin = **Qwen2.5-14B-Instruct Q4_K_M** via `./forgesre fetch-llm`. llama.cpp on `:8088`. Compose **`-c 8192`**. Timeout **90s**. Do **not** swap the default to 1.5B and do **not** switch the product default runtime to Ollama. Ollama remains YAML `ai.llm.mode: external` + `url: http://127.0.0.1:11434/v1`.

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

---

## 8. Known leftovers

- Many remote `origin/cursor/*-05f8` branches still exist and are **already merged to `main`**.
- Core slim image may not include `ping`; GUI ICMP then shows that honestly. `./forgesre verify` probes ICMP from the **host** (same as `./forgesre ping`).
- Scheduled `/ops` reports are still plain text.
- Old backups already on the VM as `data/backups/forgesre-*.tar.gz` are still valid; new runs write folders.
- Grafana deep-link from an asset is still later.
- Prometheus global rules may still fire for a host whose ForgeSRE alarm is disabled or raised; ForgeSRE will not open the incident when the webhook carries the value.
- Alloy still only ships appliance Core logs as `forge-demo-01`. Real hosts have no Loki until that changes. Limitation: **no host logs shipped**.
- LLM rewrite can still occupy the single job loop for up to `timeout_seconds` (default 90) after reports in that pass have already run. 14B Q4 on CPU may not finish inside 90s; that is why N asked about Ollama / a smaller model — wait for N before changing the pin again.
- `config/forgesre.yml` on a live VM is gitignored; a 600s timeout already written there is not overwritten by `update`. Lower it by hand if the worker still blocks.
