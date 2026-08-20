# ForgeSRE

Offline-first, self-hosted SRE console for physical data-center infrastructure.

V0.1 is a working vertical slice: install, login, demo asset, Prometheus metrics, Alertmanager → incident, logs, read-only AI investigation, playrules, playbooks, doctor, backup.

It does **not** replace Prometheus, Grafana, Loki, or NetBox. It sits on top of them.

## Quick start

On a Linux host with Docker, Docker Compose, Bash, and Git:

```bash
git clone <repo>
cd forge-sre
./install.sh
```

Non-interactive (CI / first lab):

```bash
./install.sh --non-interactive --profile standard --port 8080
./forgesre demo
```

Then open `http://127.0.0.1:8080` and sign in with the credentials from `installation-report.md`.

## What you get

| Path | Purpose |
|---|---|
| `/` | Dashboard |
| `/assets` | Inventory (local; NetBox is V0.2) |
| `/incidents` | Alertmanager-created incidents |
| `/ai/{id}` | Investigation / RCA |
| `/playrules` `/playbooks` `/escalation` | Deterministic workflow |
| `/health-ui` | Doctor |
| `/admin` | Users and audit |

Host tools:

```bash
./install.sh
./doctor.sh
./backup.sh
./update.sh
./forgesre demo
```

## Stack

Python FastAPI core + Jinja2 UI, PostgreSQL, Prometheus, Alertmanager, Loki, Grafana Alloy, Grafana. Optional llama.cpp if you place a GGUF at `$FORGESRE_DATA/models/model.gguf`.

AI never changes infrastructure.

## Docs

- V0.1 plan and stack: [`docs/v0.1.md`](docs/v0.1.md)
- Longer-term architecture: [`docs/architecture.md`](docs/architecture.md)
