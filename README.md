# ForgeSRE

Offline-first, self-hosted SRE console for physical data-center infrastructure.

V0.1 is a working vertical slice: install, login, demo asset, Prometheus metrics, Alertmanager → incident, logs, read-only AI investigation, playrules, playbooks, doctor, backup.

V0.2 adds discovery (Approve / Ignore), Prometheus HTTP SD, and optional external NetBox.

V0.3 adds a read-only RCA foundation (ForgeRCA): facts vs hypotheses, evidence IDs, optional local LLM, no infrastructure changes.

It does **not** replace Prometheus, Grafana, Loki, or NetBox. It sits on top of them.

Operator install and config (Ubuntu / vCenter VM): [`docs/install-config.md`](docs/install-config.md). Day-2 (users, servers, playrules, incidents): [`docs/operator-handbook.md`](docs/operator-handbook.md).

## Quick start

On a Linux host with Docker, Docker Compose, Bash, and Git:

```bash
git clone https://github.com/nsimic2022/forgesre.git
cd forgesre
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
| `/assets` | Inventory (local, discovery, or external NetBox) |
| `/discovery` | New device candidates (Approve / Ignore) |
| `/incidents` | Alertmanager-created incidents |
| `/ai/{id}` | Investigation / RCA (facts, hypotheses, evidence chain) |
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
./forgesre demo-rca
./forgesre fetch-llm
```

## Stack

Python FastAPI core + Jinja2 UI, PostgreSQL, Prometheus, Alertmanager, Loki, Grafana Alloy, Grafana. Optional llama.cpp: `./forgesre fetch-llm` downloads a GGUF into `$FORGESRE_DATA/models/` (not stored in git).

AI never changes infrastructure.

## Docs

- Install and config (Ubuntu / vCenter): [`docs/install-config.md`](docs/install-config.md)
- Operator handbook (users, servers, playrules, incidents): [`docs/operator-handbook.md`](docs/operator-handbook.md)
- V0.1 plan and stack: [`docs/v0.1.md`](docs/v0.1.md)
- V0.2 discovery and inventory: [`docs/v0.2.md`](docs/v0.2.md)
- V0.3 RCA foundation: [`docs/v0.3.md`](docs/v0.3.md)
- Longer-term architecture: [`docs/architecture.md`](docs/architecture.md)
