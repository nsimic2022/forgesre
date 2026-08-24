# Verify the appliance

`./forgesre doctor` is the short health light (same as **System Health** in the UI).

`./forgesre test` is the **long verification of the appliance**. It probes host, files, Compose, HTTP endpoints, login, product APIs, email YAML, optional LLM/mailbox, and recent Core log errors. It writes a Markdown table plus JSON so you can keep a record of what works and how it was tested.

Live **inventory** communication is a different command: **`./forgesre verify`** (Assets → Verify). That is ICMP / exporter or SNMP → Prometheus `up`, not this appliance report. See [`cli.md`](cli.md) § Verify.

```bash
./forgesre test
./test.sh
./forgesre test --out /tmp/forgesre-test.md
```

Reports land in `data/reports/forgesre-test-<timestamp>.md` and `.json` (gitignored with `data/`).

## How to read the report

| Status | Meaning |
|---|---|
| **PASS** | Probe succeeded |
| **WARN** | Degraded or noisy (for example Core log hits, doctor DEGRADED, SMTP enabled but no username) |
| **FAIL** | Needs a fix. Process exit code is `1` |
| **SKIP** | Feature is off on purpose (LLM profile empty, mailbox off, SMTP `enabled: false`) |

Each row has:

- **Check** — stable name (`http.core_health`, `api.login`, …)
- **Detail** — what was observed
- **How to test** — the command the script used (or the equivalent curl)
- **Fix** — first thing to try

The script **does not send email**, **does not run `./install.sh`**, and **does not change config**.

## What it covers

1. Host: Python, Docker daemon, Compose, free disk
2. Files: `.env`, `secrets/secrets.env` mode, `config/forgesre.yml`, generated Prometheus/Alertmanager/snmp
3. Secrets: shipped-default `SECRET_KEY` / webhook token (Core refuses those)
4. Compose: `docker compose ps`, Core running
5. HTTP: Core `/health`, Prometheus, Alertmanager, snmp_exporter, Loki, Alloy, Grafana, optional llama.cpp `:8088` (`/v1/models` + container health), optional Roundcube. LLM implementation: [`llm.md`](llm.md).
6. Doctor API (Bearer webhook token)
7. Login as install admin, then assets / incidents / history / jobs / journal / Administration / Email & reports
8. Prometheus HTTP SD and SNMP HTTP SD
9. Email YAML (Gmail / Outlook / local) without sending
10. `./forgesre version` and `help`
11. Last 80 Core log lines for `error` / `exception` / `traceback`
12. When profile `ai` is on: GGUF size, `ai.llm` YAML, llama.cpp health inspect, last LLM log errors

## doctor vs test vs pytest

| Command | When |
|---|---|
| `./forgesre doctor` | Every morning / after `update`. Fast. |
| `./forgesre test` | After install, after `git pull`, before you trust mail/RCA. Writes a file. |
| `pytest tests` | Developer laptop. `pip install -r requirements-dev.txt` then `PYTHONPATH=backend:agents pytest tests`. Core image does not install pytest. See [`CONTRIBUTING.md`](../CONTRIBUTING.md). |

## After an update

```bash
git checkout main
git pull origin main
./forgesre update
./forgesre test
```
