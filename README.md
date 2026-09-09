# ForgeSRE

Self-hosted SRE console for a **physical data center**.

One Ubuntu VM (a vCenter guest is the usual lab). Docker Compose, host networking, offline-friendly. You keep Prometheus, Grafana, Loki, and NetBox — ForgeSRE sits **on top of them** and turns alerts into incidents with an owner, a playbook, and a read-only AI investigation.

It is not a Kubernetes platform, not APM, and not auto-remediation. Playbooks are checklists. AI never SSH-es, never runs commands, never writes NetBox.

**Code lives on [`main`](https://github.com/nsimic2022/forgesre).** Current product: V0.7.

---

## What the system does

When a Linux server, a Windows server, or a switch is in inventory and monitoring is wired, the path is:

```
Find or enter a host
        ↓
   Assets (who owns it, how to scrape it)
        ↓
   Prometheus  — Linux: node_exporter :9100
               — Windows: windows_exporter :9182
   snmp_exporter — network: UDP/161
        ↓
   Alert → incident
        ↓
   Playrule picks a playbook + escalation
        ↓
   Who to call  +  ForgeRCA (facts / hypotheses / evidence)
```

| Piece | Role |
|---|---|
| **Discovery** | Light probe: TCP 22/80/443/9100/9182 plus SNMP GET on UDP/161, then HTTP `/metrics` on :9182/:9100 to default Windows vs Linux. Approve or Ignore. Optional read-sync from bundled NetBox (`:8001`) or `--netbox-url`. |
| **Inventory** | Hostname, IP, type (default **Auto (detect exporter)**), owner email/phone. Analysts can add and edit. |
| **Monitoring** | Prometheus HTTP SD for Linux (`node_exporter` :9100) and Windows (`windows_exporter` :9182). Bundled snmp_exporter for network devices. Grafana for graphs. |
| **Incidents** | Alertmanager webhook opens `INC-…`. Fingerprint is alert + asset. |
| **History** | `/history` — last 90 days in Postgres, plus mail/audit/notes on the incident. |
| **Playrules / playbooks** | Deterministic mapping: this alert → this checklist. Nothing is executed. |
| **Escalation** | Generated mail to the **asset owner** (SMTP optional: Gmail, Outlook, or later the off-by-default mailbox profile). |
| **ForgeRCA / ForgeAI** | Read-only investigation. ForgeRCA (Python builtin) always first; ForgeAI is the optional local LLM rewrite. |
| **Journal** | `/journal` — per-module ok/warn/error, not Docker logs and not a bash shell. |

Demo asset `forge-demo-01` is seeded so the first hour is visible without a real customer VM.

---

## What it is not

- A replacement for Prometheus, Grafana, Loki, or NetBox
- An installer for `node_exporter` or `windows_exporter` on your servers
- Auto-remediation, SSH, or executed runbooks
- A cloud LLM (local llama.cpp is optional)
- Kubernetes / tracing / APM

If you need graphs, open Grafana (`:3000`). If you need “what broke, who to call, what to check”, stay in ForgeSRE (`:8080`).

---

## Quick start

Host needs Docker, Docker Compose, Bash, and Git.

```bash
git clone https://github.com/nsimic2022/forgesre.git
cd forgesre
./install.sh --non-interactive --profile standard --port 8080
```

Sign in at `http://<VM-IP>:8080` with the credentials in `installation-report.md` (also `secrets/secrets.env`). Dashboard → **Run demo** (admin, top right) → `forge-demo-01`. Then:

```bash
./forgesre demo          # live HighCPU + mail to the asset owner
./forgesre demo-reset    # lower the demo gauges when you are done
./forgesre doctor        # short health lights
./forgesre test          # detailed report → data/reports/
./forgesre help
./forgesre secrets-check
```

**Do not re-run `./install.sh` on a live box** — it regenerates passwords. Updates:

```bash
git pull origin main
./forgesre update
```

Network gear: Assets → type **Network device** + IP, then `./forgesre snmp`. Linux stays on node_exporter `:9100`. Windows uses windows_exporter `:9182` (not node_exporter).

---

## Install / config

| When | Command |
|---|---|
| **New box** | `./install.sh` (once). Guided wizard, or `--non-interactive --profile standard --port 8080`. |
| **Live box** | `git pull origin main && ./forgesre update`. Never `./install.sh` again (it regenerates passwords). |

`./install.sh` writes the live files. If you are not using the installer, copy the template:

```bash
cp config/forgesre.example.yml config/forgesre.yml
```

Live `config/forgesre.yml`, `.env`, and `secrets/secrets.env` are **gitignored**. Do not commit passwords, tokens, or SMTP secrets. Core reads `config/forgesre.yml`, not the example. After YAML edits: `docker compose up -d --force-recreate core`.

Longer Ubuntu / vCenter manual: [docs/install-config.md](docs/install-config.md).

### Optional local LLM (download + activate)

ForgeRCA (Python) always investigates. The GGUF is **optional** — skip this on a 4 GB VM. Default pin is **Qwen2.5-14B-Instruct Q4_K_M** (~9 GB on disk; ~16 GB RAM with the rest of the stack). Not stored in git. Ollama is not the product default. Mailbox stays opt-in (`./forgesre mailbox`); do not add profile `mailbox` just to turn AI on.

**Where the file lands:** `$FORGESRE_DATA/models/model.gguf` (default **`./data/models/model.gguf`**). The filename **must** be `model.gguf`. A `wget` in the clone root does **not** count.

Usual path on an existing install (`scripts/fetch-llm.sh`):

```bash
./forgesre fetch-llm
```

That downloads into `data/models/model.gguf` (unless a file larger than 1 GB is already there), sets `COMPOSE_PROFILES=ai` in `.env`, writes `ai.enabled: true` and `ai.llm.mode: bundled` in `config/forgesre.yml`, and starts Compose service `llm` (`127.0.0.1:8088`). Then wait until llama.cpp answers and check doctor:

```bash
curl -fsS http://127.0.0.1:8088/v1/models
./forgesre doctor          # llm: ok
```

First load after `up -d llm` takes minutes (GGUF mmap). Do not re-run `./install.sh` to add the model.

If you only downloaded (`./forgesre fetch-llm --download-only`) or copied a GGUF yourself, **activate**:

1. `.env`: `COMPOSE_PROFILES=ai` (or `ai,mailbox` only if the mailbox profile is already on).
2. `config/forgesre.yml` (copy keys from `config/forgesre.example.yml` if the `ai:` block is missing): `ai.enabled: true`, `ai.llm.mode: bundled`. Timeout default is `timeout_seconds: 90` (lab 4B; raise toward 600 only for slow 14B CPU).
3. `docker compose --profile ai up -d llm` then `docker compose up -d --force-recreate core`.
4. Wait for `http://127.0.0.1:8088/v1/models`, then `./forgesre doctor`.

**Lab / 8 GB RAM** — Qwen3-4B Q4_K_M (~2.5 GB), still documented in `scripts/fetch-llm.sh --help`:

```bash
mkdir -p data/models
wget -O data/models/model.gguf \
  https://huggingface.co/Qwen/Qwen3-4B-GGUF/resolve/main/Qwen3-4B-Q4_K_M.gguf
./forgesre fetch-llm --offline
```

`--offline` skips Hugging Face; the file must already exist at `data/models/model.gguf` and be larger than 1 GB. Details: [docs/llm.md](docs/llm.md).

---

## UI

| URL | What you do |
|---|---|
| `/` | Dashboard: counts, HOST DOWN banner, **Run demo** (admin). Full doctor grid is **System Health**. |
| `/assets` | Inventory and owner contacts |
| `/discovery` | **Save & scan** / **Scan now** queue a background job (YAML ∪ auto-detected CIDRs) / Approve / Ignore; **Sync NetBox** beside it (read-only, admin). Empty NetBox = yellow. |
| `/incidents` | Open/firing incidents (**10 per page**). Archive is History. |
| `/history` | 90-day lookback, filters, closed rows |
| `/ai/INC-…` | Read-only RCA (ForgeRCA first, ForgeAI rewrite-only) |
| `/playrules` `/playbooks` `/escalation` | Workflow |
| `/journal` | Internal console |
| `/health-ui` | Same checks as `./forgesre doctor`; Open Grafana / Prometheus Targets / … |
| `/ops` | Email & reports: address book, send, outbox, scheduled reports (Edit / Clone / Remove / Enable) |
| `/admin` | Users: click a row to edit or remove; platform backup / import; audit |

Roles: super admin (install user), system admin, analyst (inventory + playrules), engineer (deep RCA), viewer.

---

## CLI

`./forgesre help` is the index. `./forgesre help snmp` (or any command) has examples.

Type `./forgesre` with no arguments for a prompt, then `journal`, `incidents`, `history` — you do not retype `./forgesre` each time. Leave with `quit`, `exit`, or Ctrl-D (`./forgesre help quit`). TAB completes command names, `logs snmp-exporter`, and incident ids. `./f` is the same CLI with a shorter name (`./f journal`). SSH to the VM first; `./forgesre login` is the ForgeSRE engineer/analyst user (not the Linux account).

```bash
./forgesre                 # prompt; then: incidents … quit
./forgesre help quit
./forgesre login
./forgesre incidents
./forgesre incidents INC-000012
./f journal
./forgesre doctor
./forgesre test
./forgesre assets
./forgesre snmp
./forgesre sd
./forgesre history
./forgesre jobs
./forgesre logs core
./forgesre journal
./forgesre render-monitoring   # after git pull, refresh Prometheus/SNMP/alerts
./forgesre backup
./forgesre backup --no-secrets
./forgesre backup --include-models
./forgesre restore data/backups/backup_YYYYMMDDTHHMMSSZ --yes
./forgesre fetch-llm           # optional ~9 GB → data/models/model.gguf
./forgesre mailbox             # optional Roundcube later; Core SMTP unchanged
```

---

## Stack

Python 3.12 + FastAPI + Jinja2 (one Core process), PostgreSQL, Prometheus, Alertmanager, snmp_exporter, Loki, Grafana Alloy, Grafana. Optional llama.cpp. Optional on-box mailbox (docker-mailserver + Roundcube) via Compose profile `mailbox` — off until `./forgesre mailbox`; that does not rewrite Gmail/Outlook SMTP. Default Compose services use **host networking**; the mailbox profile uses a bridge network and publishes 25 / 993 / Roundcube.

Config: `config/forgesre.yml` (behavior), `.env` (ports/paths), `secrets/secrets.env` (passwords, SNMP community, tokens). Never commit the last two or `data/`.

---

## Docs

Install / config (including optional LLM) is in this README above. Longer Ubuntu manual: [docs/install-config.md](docs/install-config.md). Learning path: [docs/README.md](docs/README.md). Why/when: [handbook](docs/operator-handbook.md). Commands: [cli.md](docs/cli.md). LLM details: [docs/llm.md](docs/llm.md).

Longer-term design notes (not a runtime guide): [architecture.md](docs/architecture.md). Security notes: [SECURITY.md](SECURITY.md). License: [Apache-2.0](LICENSE).
