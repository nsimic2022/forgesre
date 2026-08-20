# ForgeSRE

Offline-first, self-hosted SRE console for physical data-center infrastructure.

V0.1 is a working vertical slice: install, login, demo asset, Prometheus metrics, Alertmanager → incident, logs, read-only AI investigation, playrules, playbooks, doctor, backup.

V0.2 adds discovery (Approve / Ignore), Prometheus HTTP SD, and optional external NetBox.

V0.3 adds a read-only RCA foundation (ForgeRCA): facts vs hypotheses, evidence IDs, optional local LLM, no infrastructure changes.

V0.4 adds owner contacts on assets, lets analysts add inventory, routes escalation to the asset owner, and ships a first-hour dashboard walkthrough.

V0.5 adds a bundled **snmp_exporter** container. Network devices with an IP are walked on UDP/161; Linux hosts stay on node_exporter `:9100`.

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

Then open `http://127.0.0.1:8080` and sign in with the credentials from `installation-report.md`. The dashboard walkthrough is the first-hour demo: `forge-demo-01` already has owner contacts and a closed HighCPU history row. `./forgesre demo` opens a live incident and generates mail to the asset owner. **Console** (`/journal`) shows whether seed, demo, inventory, and notifications succeeded.

Network gear: Assets → type **Network device** + IP, then `./forgesre snmp`. CLI index: `./forgesre help`.

## What you get

| Path | Purpose |
|---|---|
| `/` | Dashboard |
| `/assets` | Inventory (local, discovery, or external NetBox). Owner contacts. Analysts can add. |
| `/discovery` | New device candidates (Approve / Ignore) |
| `/incidents` | Alertmanager-created incidents |
| `/ai/{id}` | Investigation / RCA (facts, hypotheses, evidence chain) |
| `/playrules` `/playbooks` `/escalation` | Deterministic workflow |
| `/journal` | Internal console: per-module ok/error reports |
| `/health-ui` | Doctor |
| `/admin` | Users and audit |

Host tools:

```bash
./forgesre help
./forgesre help snmp
./install.sh                  # new VM only
./forgesre doctor
./forgesre assets
./forgesre snmp
./forgesre render-monitoring  # existing VM after git pull
./forgesre demo
./forgesre demo-rca
./forgesre journal
./forgesre fetch-llm
./forgesre backup
./forgesre update
```

## Stack

Python FastAPI core + Jinja2 UI, PostgreSQL, Prometheus, Alertmanager, **snmp_exporter**, Loki, Grafana Alloy, Grafana. Optional llama.cpp: `./forgesre fetch-llm` downloads a GGUF into `$FORGESRE_DATA/models/` (not stored in git).

AI never changes infrastructure.

## Docs

- Install and config (Ubuntu / vCenter): [`docs/install-config.md`](docs/install-config.md)
- Operator handbook (users, servers, playrules, incidents): [`docs/operator-handbook.md`](docs/operator-handbook.md)
- V0.1 plan and stack: [`docs/v0.1.md`](docs/v0.1.md)
- V0.2 discovery and inventory: [`docs/v0.2.md`](docs/v0.2.md)
- V0.3 RCA foundation: [`docs/v0.3.md`](docs/v0.3.md)
- V0.4 asset contacts and first-hour demo: [`docs/v0.4.md`](docs/v0.4.md)
- V0.5 bundled snmp_exporter: [`docs/v0.5.md`](docs/v0.5.md)
- Longer-term architecture: [`docs/architecture.md`](docs/architecture.md)
