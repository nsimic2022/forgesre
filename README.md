# ForgeSRE

Self-hosted SRE console for a **physical data center**.

One Ubuntu VM (a vCenter guest is the usual lab). Docker Compose, host networking, offline-friendly. You keep Prometheus, Grafana, Loki, and optional NetBox — ForgeSRE sits **on top of them** and turns alerts into incidents with an owner, a playbook, and a read-only AI investigation.

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
| **Discovery** | Light probe: TCP 22/80/443/9100/9182 plus SNMP GET on UDP/161, then HTTP `/metrics` on :9182/:9100 to default Windows vs Linux. Approve or Ignore. Optional read-sync from an existing NetBox. |
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
- A bundled NetBox or a cloud LLM
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

Sign in at `http://<VM-IP>:8080` with the credentials in `installation-report.md` (also `secrets/secrets.env`). Dashboard → first-hour walkthrough → `forge-demo-01`. Then:

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

## UI

| URL | What you do |
|---|---|
| `/` | Dashboard, doctor lights, first-hour walkthrough |
| `/assets` | Inventory and owner contacts |
| `/discovery` | Approve / Ignore new devices |
| `/incidents` | Alertmanager incidents (recent 200) |
| `/history` | 90-day lookback, filters, closed rows |
| `/ai/INC-…` | Read-only RCA |
| `/playrules` `/playbooks` `/escalation` | Workflow |
| `/journal` | Internal console |
| `/health-ui` | Same checks as `./forgesre doctor`; Open Grafana / Prometheus / … |
| `/ops` | Email & reports: address book, send, outbox, scheduled reports |
| `/admin` | Users: click a row to edit or remove; audit |

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
./forgesre fetch-llm           # optional ~9 GB GGUF, not stored in git
./forgesre mailbox             # optional Roundcube later; Core SMTP unchanged
```

---

## Stack

Python 3.12 + FastAPI + Jinja2 (one Core process), PostgreSQL, Prometheus, Alertmanager, snmp_exporter, Loki, Grafana Alloy, Grafana. Optional llama.cpp. Optional on-box mailbox (docker-mailserver + Roundcube) via Compose profile `mailbox` — off until `./forgesre mailbox`; that does not rewrite Gmail/Outlook SMTP. Default Compose services use **host networking**; the mailbox profile uses a bridge network and publishes 25 / 993 / Roundcube.

Config: `config/forgesre.yml` (behavior), `.env` (ports/paths), `secrets/secrets.env` (passwords, SNMP community, tokens). Never commit the last two or `data/`.

---

## Docs

**Operators (start here)**

- [Install and config (Ubuntu / vCenter)](docs/install-config.md)
- [Operator handbook (users, servers, playrules, incidents, CLI)](docs/operator-handbook.md)
- [Verify the appliance](docs/verify.md) (`./forgesre test`)
- [Operator CLI](docs/cli.md)
- [Local LLM](docs/llm.md)
- [Docs index](docs/README.md)

**What each release shipped** (optional): [V0.1](docs/v0.1.md) · [V0.2](docs/v0.2.md) · [V0.3](docs/v0.3.md) · [V0.4](docs/v0.4.md) · [V0.5 snmp_exporter](docs/v0.5.md) · [V0.6 hardening](docs/v0.6.md) · [V0.7 history](docs/v0.7.md)

Longer-term design notes (not a runtime guide): [architecture.md](docs/architecture.md). Security notes: [SECURITY.md](SECURITY.md). License: [Apache-2.0](LICENSE).
