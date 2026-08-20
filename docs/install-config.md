# ForgeSRE install and config

Operator guide for a **single Ubuntu Linux host**. A vCenter VM is the intended lab shape. ForgeSRE is an appliance: Docker Compose + **host networking**. It is not a Kubernetes install.

Code: https://github.com/nsimic2022/forgesre (`main`).

1. [What you are installing](#1-what-you-are-installing)
2. [VM sizing](#2-vm-sizing)
3. [vCenter VM](#3-vcenter-vm)
4. [Host packages](#4-host-packages)
5. [Clone and install](#5-clone-and-install)
6. [Open the UI](#6-open-the-ui)
7. [Config files](#7-config-files-what-goes-where)
8. [`config/forgesre.yml`](#8-configforgesreyml)
9. [`.env`](#9-env-deployment)
10. [`secrets/secrets.env`](#10-secretssecretsenv)
11. [Day-2 commands](#11-day-2-commands)
12. [Optional local LLM](#12-optional-local-llm)
13. [Common failures](#13-common-failures)

---

## 1. What you are installing

| Piece | Role | Listens |
|---|---|---|
| Core (FastAPI + UI) | Product UI and API | `0.0.0.0:8080` (changeable) |
| Grafana | Deep dashboards | host port `3000` |
| PostgreSQL | ForgeSRE database | `127.0.0.1:5432` |
| Prometheus | Metrics | `127.0.0.1:9090` |
| Alertmanager | Alert webhook → incidents | `127.0.0.1:9093` |
| Loki + Alloy | Logs as evidence | `127.0.0.1:3100` / `12345` |
| llama.cpp (optional) | Local LLM | `127.0.0.1:8088` |

From a laptop you open **Core :8080** and **Grafana :3000**. The rest stay on localhost on the VM.

AI is read-only. It never SSH-es, never runs playbooks, never writes NetBox.

---

## 2. VM sizing

Installer preflight:

- 2 CPU (4 is more comfortable)
- 4 GB RAM (8 GB recommended)
- 10 GB free disk (40 GB recommended)
- Ubuntu Server 22.04 or 24.04 LTS
- Outbound internet on first install (GitHub + container registries)

Nested virtualization is **not** required.

---

## 3. vCenter VM

Create a VM from an Ubuntu Server 22.04 or 24.04 ISO (or a golden template).

| Setting | Value |
|---|---|
| Guest OS | Ubuntu Linux (64-bit) |
| vCPU | 2 (4 better) |
| Memory | 8 GB recommended |
| Disk | 40 GB thin is enough for a lab |
| NIC | VMXNET3 on the **management** network |
| Firmware | BIOS or EFI both fine |
| Nested HV | Off |

Give the VM a **static IP** (or a DHCP reservation). Note the IP; laptops will use `http://<VM-IP>:8080`, not `127.0.0.1`.

After first boot, set hostname and timezone, then continue with host packages below.

Do not install Kubernetes, snap MicroK8s, or extra reverse proxies for V0.x. One Linux host, host networking.

---

## 4. Host packages

```bash
sudo apt update
sudo apt install -y ca-certificates curl git docker.io docker-compose-v2
sudo systemctl enable --now docker
sudo usermod -aG docker "$USER"
```

Log out and back in, then:

```bash
docker info
docker compose version
timedatectl   # optional: sudo timedatectl set-timezone Europe/Belgrade
```

If `docker info` needs `sudo`, `./install.sh` still works via `sudo docker compose`.

---

## 5. Clone and install

```bash
git clone https://github.com/nsimic2022/forgesre.git
cd forgesre
```

Non-interactive lab:

```bash
./install.sh --non-interactive --profile standard --port 8080
```

Guided wizard:

```bash
./install.sh
```

Useful flags:

| Flag | Meaning |
|---|---|
| `--non-interactive` | No prompts; use flags/defaults |
| `--profile standard\|full-ai` | Standard = no LLM download. `full-ai` downloads ~9 GB GGUF and starts llama.cpp |
| `--timezone ZONE` | Default `Europe/Belgrade` |
| `--data-dir PATH` | Default `./data` |
| `--port N` | Core UI/API port (default `8080`) |
| `--enable-ai yes\|no` | `yes` downloads the GGUF (same as full-ai). ForgeRCA still works without it |
| `--enable-discovery yes\|no` | Default yes |
| `--discovery-cidrs 10.20.30.0/24,10.10.0.0/24` | TCP probe, max 256 hosts, skip loopback |
| `--netbox-url URL` | External NetBox only; token goes in secrets |
| `--offline` | Do not pull images (images must already exist) |

Do **not** re-run `./install.sh` on an existing instance unless you intend to **regenerate passwords and tokens**.

---

## 6. Open the UI

On the VM, credentials are in `installation-report.md` and `secrets/secrets.env`.

From a laptop, use the **VM IP**, not `127.0.0.1`:

- ForgeSRE: `http://<VM-IP>:8080`
- Grafana: `http://<VM-IP>:3000` (user `admin`)

Firewall example:

```bash
sudo ufw allow OpenSSH
sudo ufw allow 8080/tcp
sudo ufw allow 3000/tcp
sudo ufw enable
```

Also allow those ports on the vCenter / NSX / physical firewall toward the management network only.

Smoke test on the VM:

```bash
./doctor.sh
./forgesre demo
./forgesre demo-rca
```

`./doctor.sh` should report **HEALTHY**. LLM may be `disabled` — that is OK without a GGUF.

Then in the UI:

1. Login → Dashboard **First-hour walkthrough**.
2. Assets → `forge-demo-01` (owner contacts + closed HighCPU history).
3. `./forgesre demo` or **Run demo workflow** → new incident **Who to call** → Escalation (generated mail to `platform@forgesre.local`).
4. Discovery (`10.20.30.41`) if you want Approve/Ignore.

Nothing in that path is a real customer server.

---

## 7. Config files (what goes where)

Three files. Do not mix them.

| File | Purpose | Git |
|---|---|---|
| `.env` | Deployment: ports, data dir, compose, generated Prometheus/Alertmanager paths | ignored |
| `config/forgesre.yml` | Features: discovery, NetBox URL, AI/RCA, Loki, Grafana | ignored (example is committed) |
| `secrets/secrets.env` | Passwords and tokens | ignored, mode `600` |

Template for YAML: [`config/forgesre.example.yml`](../config/forgesre.example.yml).

Show current YAML:

```bash
./forgesre config
```

After editing YAML, restart Core (settings load at process start):

```bash
docker compose up -d --force-recreate core
# if docker needs root:
sudo docker compose up -d --force-recreate core
```

After editing generated Prometheus/Alertmanager YAML:

```bash
curl -fsS -X POST http://127.0.0.1:9090/-/reload
```

---

## 8. `config/forgesre.yml`

```yaml
system:
  mode: online          # online | offline
  timezone: Europe/Belgrade
  log_level: info

inventory:
  provider: local       # local | netbox
  netbox:
    enabled: false      # true only for an existing NetBox
    mode: external      # never bundled
    url: "https://netbox.example.local"

discovery:
  enabled: true
  mode: semi-automatic  # manual | semi-automatic | automatic
  cidrs: ["10.20.30.0/24"]

monitoring:
  prometheus:
    url: http://127.0.0.1:9090
  alertmanager:
    url: http://127.0.0.1:9093

logging:
  loki:
    enabled: true
    url: http://127.0.0.1:3100

grafana:
  enabled: true
  url: http://127.0.0.1:3000

ai:
  enabled: false
  llm:
    mode: disabled      # bundled | external | disabled
    url: http://127.0.0.1:8088/v1
    model: local
  rca:
    engine: forgerca
    window_minutes: 30
    max_log_lines: 20
    max_evidence: 40

notifications:
  email:
    enabled: false
    host: smtp.example.local
    port: 587
    from: forgesre@example.local
    tls: true
```

Notes:

- **Discovery** is a TCP probe (22/80/443/161/9100), not nmap. New hosts wait on `/discovery` for Approve / Ignore unless `mode: automatic` (still audited).
- **NetBox** is read-sync only. Put the token in `NETBOX_API_TOKEN`, never in YAML.
- **RCA** works with `ai.enabled: false` (builtin analyst). Set `ai.enabled: true` only if you have a local OpenAI-compatible endpoint or a GGUF.
- Changing `cidrs` does not require a reinstall. Restart Core.

---

## 9. `.env` (deployment)

Written by `./install.sh`. Typical keys:

```bash
FORGESRE_VERSION=0.4.0
FORGESRE_DATA=./data
FORGESRE_TIMEZONE=Europe/Belgrade
FORGESRE_HTTP_PORT=8080
GRAFANA_PORT=3000
FORGESRE_PROFILE=standard
COMPOSE_PROFILES=          # set to ai only when a GGUF is present
POSTGRES_PASSWORD=...      # must match secrets
GRAFANA_ADMIN_PASSWORD=...
ALERTMANAGER_CONFIG=./data/generated/alertmanager.yml
PROMETHEUS_CONFIG=./data/generated/prometheus.yml
```

Change the UI port here **and** regenerate Prometheus/Alertmanager with the same port, then recreate Core and reload Prometheus.

---

## 10. `secrets/secrets.env`

```bash
POSTGRES_PASSWORD=
FORGESRE_ADMIN_EMAIL=admin@forgesre.local
FORGESRE_ADMIN_PASSWORD=
GRAFANA_ADMIN_PASSWORD=
ALERTMANAGER_WEBHOOK_TOKEN=   # also used for Prometheus HTTP SD
SECRET_KEY=
SMTP_USERNAME=
SMTP_PASSWORD=
NETBOX_API_TOKEN=
```

Directory `secrets/` should be `700`, file `600`. Never commit it.

---

## 11. Day-2 commands

```bash
./doctor.sh              # health
./forgesre status        # compose ps
./forgesre logs core     # container logs
./forgesre demo          # HighCPU + owner notification + similar-incident history
./forgesre demo-rca      # filesystem RCA demo (does not fill a real disk)
./forgesre fetch-llm     # download GGUF (~9 GB) and start llama.cpp; do not re-run install.sh
./backup.sh              # Postgres + config tarball under $FORGESRE_DATA/backups
./update.sh              # backup, refresh, restart, doctor
```

---

## 12. Local LLM (downloaded, not in git)

ForgeSRE does **not** store the GGUF in the repository. Install (or `./forgesre fetch-llm` on an existing box) pulls **Qwen2.5-14B-Instruct Q4_K_M** (~9 GB) into `$FORGESRE_DATA/models/model.gguf`.

New VM:

```bash
./install.sh --non-interactive --profile full-ai --port 8080
```

Already installed (**do not** re-run `./install.sh` — that regenerates passwords):

```bash
./forgesre fetch-llm
./doctor.sh
```

Override the URL with `FORGESRE_LLM_URL` if Hugging Face is blocked. For a fully offline box, copy a CPU Instruct GGUF to `data/models/model.gguf` (that name) then run `./forgesre fetch-llm` — it will skip the download if the file is already large enough.

Without a GGUF, ForgeRCA still runs the builtin analyst on Prometheus/Loki evidence. Cloud LLMs are not required.

Doctor `llm: disabled` means the model/container is off. `llm: ok` means llama.cpp answered. RCA never executes playbooks.

---

## 13. Common failures

| Symptom | What to do |
|---|---|
| Preflight: port in use | `./install.sh --port 8081` or free 8080 |
| Grafana 3000 busy | Change `GRAFANA_PORT` in `.env`, recreate Grafana |
| `docker info` denied | Add user to `docker` group or use sudo |
| Clone has no `install.sh` | You are not on `main`, or `git pull origin main` is needed |
| UI only on the VM | You used `127.0.0.1` from the laptop, or 8080 is blocked |
| Doctor: NetBox error | Disable NetBox or set URL + `NETBOX_API_TOKEN` |
| Discovery finds nothing | Empty `cidrs`, or hosts do not open 22/80/443/161/9100 |
| LLM download fails | Disk, Hugging Face, or proxy. Set `FORGESRE_LLM_URL` or copy a GGUF to `data/models/model.gguf` |
| Doctor: llm error after fetch | Wait for llama.cpp to load the GGUF, then `./doctor.sh` again |
| Re-install wiped logins | `./install.sh` regenerates secrets; use `installation-report.md` from the last run |

```bash
docker compose logs --tail 80 core
./doctor.sh
```

Day-2 operations (users, adding servers, playrules, playbooks, incidents): [`operator-handbook.md`](operator-handbook.md).
