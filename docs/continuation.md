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

**Monday 31 August 2026.** Operator N (Serbian) agreed: llama.cpp **prompt processing (prefill)** is not generation. Their Qwen3-4B on CPU is ~25–29 tok/s. A ~6157-token prompt ≈ 246s prefill, so `timeout_seconds: 300` leaves almost no time to generate → `cancel task` in `docker compose logs llm`. **Do not switch to a smaller model.** Node exporter does not talk to the LLM. Chain: Node Exporter → Prometheus → ForgeRCA gathers evidence → **large prompt** → Qwen3.

Fix implemented in code: **shrink only the payload to `OpenAICompatibleLLM.complete_json` / `prompt_context`**. Builtin ForgeRCA stays first and keeps full facts/anomalies/hypotheses/evidence IDs on the incident.

On the Ubuntu VM N uses, resume with:

```bash
git pull origin main && ./forgesre update
```

Then Investigate again with `docker compose logs -f llm` — prompt tokens should drop a lot; cancel at timeout 300 should stop. Lab without image pull: `./forgesre update --offline`. Also: `./forgesre ping`, `./forgesre verify`, `./forgesre test` (appliance health, not inventory). See [docs/cli.md](cli.md) § Verify.

Hard-refresh the browser after UI/docs changes.

**Never** re-run `./install.sh` on a live box. That regenerates passwords in `secrets/secrets.env` and will wipe the install admin the operator already uses.

---

## 2. Checked twice (pytest)

From this branch, after `pip install -r requirements-dev.txt`:

```bash
PYTHONPATH=backend:agents python3 -m pytest tests
PYTHONPATH=backend:agents python3 -m pytest tests
```

Both runs: **334 passed**, 1 warning (Starlette `httpx` / `starlette.testclient` deprecation — ignore), 41.44s then 40.27s. Python 3.12, pytest 9.x. Count recorded after the double run on `cursor/llm-prompt-shrink-05f8` (was 332 before the two new prompt-shrink tests).

If pytest fails next session: fix on a `cursor/<name>-05f8` branch, re-run **twice**, then `git merge --no-ff` to `main`. Branch pattern `cursor/<name>-05f8`.

---

## 3. Done today / on this branch

1. **`prompt_context` is a compact rewrite JSON**, not `json.dumps(sanitize(ctx))[:12000]`. Cap **5000 characters** (was 12000 ≈ 3–6k tokens). Includes incident title/severity/host, top facts, metric snapshots (`name` / `value` / `unit`), short log snippets (already capped by `max_log_lines`, further capped for the LLM), compact hypotheses/anomalies/limitations, and the builtin draft. Secrets still go through `sanitize`.
2. **Prometheus/Loki blobs are stripped**, not truncated mid-JSON: `values: [[ts, x], …]` matrices, instant-vector `[ts, "x"]` pairs, and Prom `data.result` series/label dumps are dropped. LLM should see CPU 92% / mem 81% / disk 74%, not raw series.
3. **Builtin investigation is unchanged.** Full evidence stays on the incident / `result` facts / evidence IDs / PromQL. Only the LLM user message shrinks.
4. **Did not** change llama.cpp compose, GGUF URL, catalog (catalog stays reverted), `timeout_seconds` in `config/forgesre.example.yml` (stays 90; live yml is gitignored; N already set 300). Did **not** add Celery. Did **not** switch the model pin.
5. Tests: fat fake Prom dump must not appear in the LLM payload; compact fields still include CPU/mem-like facts. No GGUF download.
6. Docs: [`docs/llm.md`](llm.md) (prefill vs generation, 5000-char cap), [`docs/operator-handbook.md`](operator-handbook.md) §13, this file.

---

## 4. What N should do on the VM

Do **not** run `./install.sh`. Do **not** wget a smaller GGUF for this.

```bash
git pull origin main && ./forgesre update
```

`./forgesre update` rebuilds Core when `agents/` changed. Then:

```bash
docker compose logs -f llm
```

Click **Run AI investigation** (or wait for the queued rewrite). Prompt tokens in llama.cpp should be far below ~6157. `cancel task` at `timeout_seconds: 300` should stop. You can leave timeout at 300; 90s is usually enough once the prompt is small.

Correct threads key in **local** (gitignored) `.env` if you still care about CPU contention:

```bash
FORGESRE_LLM_THREADS=8
```

Not `FORGESE_LLM_THREADS`. Catalog / Health model switcher is off `main`. Hard-refresh the browser.

---

## 5. Product facts not to redo

These already work on `main`. Do not “fix” them unless N asks.

- `./forgesre test` = appliance health report → `data/reports/`. `./forgesre verify` = live inventory communication.
- Theme toggle cycles **light → dark → system**. Left nav stays dark.
- Dashboard demos are **one** top-right button + a closeable panel. Demo rows stay **labeled DEMO**.
- Incident ids look like `INC-0134_16.08.2026_09:13`.
- RCA is Python under `agents/rca/`. The LLM only rewrites prose. Builtin ForgeRCA always runs first. LLM payload is compact; stored investigation is full.
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
- Bundled LLM pin = **Qwen2.5-14B-Instruct Q4_K_M** via `./forgesre fetch-llm`. llama.cpp on `:8088`. Compose **`-m /models/model.gguf -c 8192 -t "${FORGESRE_LLM_THREADS:-8}"`**. Example timeout **90s**. Live yml may be 300. Do **not** restore the GGUF catalog / Health picker. Do **not** swap the default to 1.5B / 1.7B to fix prefill cancel. Do **not** switch the product default runtime to Ollama. Ollama remains YAML `ai.llm.mode: external` + `url: http://127.0.0.1:11434/v1`. Threads are the operator’s `.env`, not a repo pin.

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
- Core slim image may not include `ping`; GUI ICMP then shows that honestly. `./forgesre verify` probes ICMP from the **host** (same as `./forgesre ping`).
- Scheduled `/ops` reports are still plain text.
- Old backups already on the VM as `data/backups/forgesre-*.tar.gz` are still valid; new runs write folders.
- Grafana deep-link from an asset is still later.
- Prometheus global rules may still fire for a host whose ForgeSRE alarm is disabled or raised; ForgeSRE will not open the incident when the webhook carries the value.
- Alloy still only ships appliance Core logs as `forge-demo-01`. Real hosts have no Loki until that changes. Limitation: **no host logs shipped**.
- LLM rewrite can still occupy the single job loop for up to `timeout_seconds` (default 90 in example.yml; N’s live yml is 300) after reports in that pass have already run. 14B Q4 on CPU may not finish inside 90s; wait for N before changing the pin again. Huge prompts + CPU 4B needed 300s **until** context was small; that path is now compact.
- `config/forgesre.yml` on a live VM is gitignored; a 300s or 600s timeout already written there is not overwritten by `update`.
