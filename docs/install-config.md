# ForgeSRE install and configuration

Operator manual for a **single Ubuntu host**. A vCenter VM is the usual lab. ForgeSRE is an appliance: Docker Compose and **host networking**. It is not Kubernetes.

Repository: https://github.com/nsimic2022/forgesre (`main`).

| Chapter | What it covers |
|---|---|
| [1. What you install](#1-what-you-install) | Containers and ports |
| [2. Sizing](#2-sizing) | CPU, RAM, disk |
| [3. vCenter guest](#3-vcenter-guest) | VM settings and open-vm-tools |
| [4. Host preparation](#4-host-preparation) | apt, Docker, clone |
| [5. First install](#5-first-install) | `./install.sh` (new VM only) |
| [6. Open the UI](#6-open-the-ui) | URLs and firewall |
| [7. Verify the appliance](#7-verify-the-appliance) | `./forgesre test` and `./forgesre doctor` |
| [8. Configuration files](#8-configuration-files) | `.env`, YAML, secrets |
| [9. `config/forgesre.yml`](#9-configforgesreyml) | Features |
| [10. `.env`](#10-env) | Deployment |
| [11. `secrets/secrets.env`](#11-secretssecretsenv) | Passwords and tokens |
| [12. Operator CLI](#12-operator-cli) | Everyday `./forgesre` commands |
| [13. Advanced CLI](#13-advanced-cli) | Logs, rebuild Core, LLM profile, git pull |
| [14. Updates](#14-updates) | Existing VM after `git pull` |
| [15. Optional local LLM](#15-optional-local-llm) | GGUF / llama.cpp — full guide: [`llm.md`](llm.md) |
| [16. Troubleshooting](#16-troubleshooting) | Common failures |

Day-to-day product work (users, inventory, incidents, email): [`operator-handbook.md`](operator-handbook.md).  
How to read a test report: [`verify.md`](verify.md).  
CLI index: [`cli.md`](cli.md).

---

## 1. What you install

| Piece | Role | Listens |
|---|---|---|
| Core (FastAPI + UI) | Product UI and API | `0.0.0.0:8080` (changeable) |
| Grafana | Graphs | host port `3000` |
| PostgreSQL | ForgeSRE database | `127.0.0.1:5432` |
| Prometheus | Metrics | `127.0.0.1:9090` |
| snmp_exporter | SNMP walks | `127.0.0.1:9116` |
| Alertmanager | Alert webhook → incidents | `127.0.0.1:9093` |
| Loki + Alloy | Logs as evidence | `127.0.0.1:3100` / `12345` |
| llama.cpp (optional) | Local LLM | `127.0.0.1:8088` |

From a laptop you open **Core :8080** and **Grafana :3000**. Everything else stays on localhost on the VM.

AI is read-only. It never SSH-es, never runs playbooks, never writes NetBox.

---

## 2. Sizing

Installer preflight:

- 2 CPU (4 is more comfortable)
- 4 GB RAM (8 GB recommended)
- 10 GB free disk (40 GB recommended)
- Ubuntu Server 22.04 or 24.04 LTS
- Outbound internet on first install (GitHub + container registries)

Nested virtualization is **not** required.

---

## 3. vCenter guest

Create a VM from an Ubuntu Server 22.04 or 24.04 ISO (or a golden template).

| Setting | Value |
|---|---|
| Guest OS | Ubuntu Linux (64-bit) |
| vCPU | 2 (4 better) |
| Memory | 8 GB recommended |
| Disk | 40 GB thin is enough for a lab |
| NIC | VMXNET3 on the **management** network |
| Firmware | BIOS or EFI |
| Nested HV | Off |

Give the VM a **static IP** (or a DHCP reservation). Laptops use `http://<VM-IP>:8080`, not `127.0.0.1`.

On a VMware guest, install tools so the hypervisor can see the VM cleanly:

```bash
sudo apt install -y open-vm-tools
sudo systemctl enable --now open-vm-tools
systemctl status vmtoolsd
```

Do not install Kubernetes, snap MicroK8s, or extra reverse proxies for V0.x.

---

## 4. Host preparation

Run as **root**, or prefix every command with `sudo`. This is the path used on the Ubuntu appliance:

```bash
apt update
apt upgrade -y
apt autoremove -y
apt install -y ca-certificates curl git docker.io docker-compose-v2
systemctl enable --now docker
usermod -aG docker "$USER"
```

Log out and back in (so the docker group applies), then:

```bash
docker info
docker compose version
timedatectl   # optional: timedatectl set-timezone Europe/Belgrade
```

Clone **main**:

```bash
git clone https://github.com/nsimic2022/forgesre.git
cd forgesre
```

If `docker info` still needs `sudo`, `./install.sh` and Compose still work via `sudo docker compose`.

---

## 5. First install

**New VM only.** Do not re-run this on a box that already has `secrets/secrets.env`.

Non-interactive lab:

```bash
./install.sh --non-interactive --profile standard --port 8080
```

Guided wizard:

```bash
./install.sh
```

| Flag | Meaning |
|---|---|
| `--non-interactive` | No prompts; use flags/defaults |
| `--profile standard\|full-ai` | Standard = no LLM download. `full-ai` downloads ~9 GB GGUF and starts llama.cpp |
| `--timezone ZONE` | Default `Europe/Belgrade` |
| `--data-dir PATH` | Default `./data` |
| `--port N` | Core UI/API port (default `8080`) |
| `--enable-ai yes\|no` | `yes` downloads the GGUF. ForgeRCA still works without it |
| `--enable-discovery yes\|no` | Default yes |
| `--discovery-cidrs 10.20.30.0/24,10.10.0.0/24` | TCP 22/80/443/9100/9182 + SNMP GET UDP/161 + HTTP /metrics on :9182/:9100 when a host is alive |
| `--netbox-url URL` | External NetBox only; token goes in secrets |
| `--offline` | Do not pull images |

After install, verify:

```bash
./forgesre test
./forgesre doctor
```

---

## 6. Open the UI

Credentials: `installation-report.md` and `secrets/secrets.env`.

From a laptop use the **VM IP**:

- ForgeSRE: `http://<VM-IP>:8080`
- Grafana: `http://<VM-IP>:3000` (user `admin`)

```bash
sudo ufw allow OpenSSH
sudo ufw allow 8080/tcp
sudo ufw allow 3000/tcp
sudo ufw enable
```

Also allow those ports on the vCenter / NSX / physical firewall toward the management network only.

First-hour path:

```bash
./forgesre demo
./forgesre demo-rca
```

Then in the UI: Dashboard **Run demo** (top right) → pick Linux HighCPU → incident on `forge-demo-01` (DEMO pill) → Who to call. `./forgesre demo-reset` (or Reset demo gauges in the same panel) lowers the demo gauges when you are done.

---

## 7. Verify the appliance

Two commands. They do not replace each other.

| Command | What it is |
|---|---|
| `./forgesre doctor` | Short lights (same as System Health). HEALTHY or DEGRADED |
| `./forgesre test` | Long report: host, files, Compose, HTTP, login, APIs, email config, Core logs |

```bash
./forgesre test
./test.sh                 # same
./forgesre doctor
```

The test writes Markdown + JSON under `data/reports/forgesre-test-<timestamp>.*` and prints the table. Exit code `1` only when a check **FAIL**s. **SKIP** means the feature is off (LLM, mailbox, SMTP). **WARN** is degraded but usable.

Details: [`verify.md`](verify.md).

---

## 8. Configuration files

Three files. Do not mix them.

| File | Purpose | Git |
|---|---|---|
| `.env` | Ports, data dir, compose profiles, generated Prometheus paths | ignored |
| `config/forgesre.yml` | Discovery, NetBox URL, AI/RCA, Loki, Grafana, SMTP host | ignored (example is committed) |
| `secrets/secrets.env` | Passwords and tokens | ignored, mode `600` |

Template: [`config/forgesre.example.yml`](../config/forgesre.example.yml).

```bash
./forgesre config
```

After editing YAML, recreate Core (settings load at process start):

```bash
docker compose up -d --force-recreate core
# after code or Dockerfile changes:
docker compose build core
docker compose up -d core
```

After editing generated Prometheus/Alertmanager YAML:

```bash
curl -fsS -X POST http://127.0.0.1:9090/-/reload
```

---

## 9. `config/forgesre.yml`

```yaml
system:
  mode: online          # online | offline
  timezone: Europe/Belgrade
  log_level: info
  cookie_secure: false  # true when Core is behind HTTPS (or FORGESRE_COOKIE_SECURE=1)

inventory:
  provider: local       # local | netbox
  netbox:
    enabled: false
    mode: external
    url: "https://netbox.example.local"

discovery:
  enabled: true
  mode: semi-automatic
  cidrs: ["10.20.30.0/24"]

monitoring:
  prometheus:
    url: http://127.0.0.1:9090
  alertmanager:
    url: http://127.0.0.1:9093
  snmp:
    enabled: true
    exporter_url: http://127.0.0.1:9116
    module: if_mib

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
    # Gmail: smtp.gmail.com  Outlook/M365: smtp.office365.com
```

- **Discovery** probes TCP **22 / 80 / 443 / 9100 / 9182** and SNMPv2c GET on **UDP/161**. Alive hosts also get HTTP GET `/metrics` on **:9182** and **:9100** so the default type/port is Windows vs Linux from exporter text, not “always Linux :9100”.
- **RCA** works with `ai.enabled: false`. Set `ai.enabled: true` only with a local OpenAI-compatible endpoint or a GGUF.
- Changing `cidrs` needs a Core recreate, not a reinstall.
- Changing `SNMP_COMMUNITY` needs `./forgesre render-monitoring`.

---

## 10. `.env`

Written by `./install.sh`.

```bash
FORGESRE_VERSION=0.7.0
FORGESRE_DATA=./data
FORGESRE_TIMEZONE=Europe/Belgrade
FORGESRE_HTTP_PORT=8080
GRAFANA_PORT=3000
FORGESRE_PROFILE=standard
COMPOSE_PROFILES=          # empty | ai | mailbox | ai,mailbox
FORGESRE_LLM_THREADS=8     # llama.cpp CPU threads (fetch-llm sets nproc-2)
```

Start the bundled LLM container later with `COMPOSE_PROFILES=ai` (or `./forgesre fetch-llm`) then:

```bash
docker compose --profile ai up -d llm
curl -fsS http://127.0.0.1:8088/v1/models
```

Details: [`llm.md`](llm.md).

---

## 11. `secrets/secrets.env`

```bash
POSTGRES_PASSWORD=
FORGESRE_ADMIN_EMAIL=admin@forgesre.local
FORGESRE_ADMIN_PASSWORD=
GRAFANA_ADMIN_PASSWORD=
ALERTMANAGER_WEBHOOK_TOKEN=
SECRET_KEY=
SMTP_USERNAME=
SMTP_PASSWORD=
SNMP_COMMUNITY=public
```

Directory `secrets/` mode `700`, file `600`. UI users created in Administration live in Postgres as bcrypt hashes — not in this file.

```bash
./forgesre secrets-check
```

---

## 12. Operator CLI

From the clone directory. `./forgesre help` is the index. `./forgesre help <command>` has examples (`./forgesre help quit` for leaving the prompt). `./f` is the same CLI. TAB completes names.

```bash
./forgesre                 # prompt; then journal / incidents / doctor / test / quit
./forgesre help quit
./forgesre login
./forgesre whoami
./forgesre doctor
./forgesre test
./forgesre status
./forgesre logs core
./forgesre config
./forgesre assets
./forgesre snmp
./forgesre sd
./forgesre incidents
./forgesre history --days 90
./forgesre jobs
./forgesre journal
./forgesre demo
./forgesre demo-reset
./forgesre secrets-check
./forgesre render-monitoring
./forgesre backup
./forgesre backup --no-secrets
./forgesre backup --include-models
./forgesre restore data/backups/backup_YYYYMMDDTHHMMSSZ --yes
./forgesre version
```

Full list and debug recipes: [`cli.md`](cli.md).

---

## 13. Advanced CLI

Use these on an **already installed** VM. Do not run `./install.sh` again.

**Compose status and Core logs** (what you actually use when RCA or LLM looks stuck):

```bash
docker compose ps
docker compose ps core
docker compose logs --tail=100 core
docker compose logs --tail=100 core | grep -iE "llm|rca|error|exception"
docker compose logs --tail=50 core | grep "/ai"
docker compose logs -f core
docker compose logs --tail=200 llm
docker compose logs -f llm
docker compose ps -q llm | xargs -r docker inspect --format='{{json .State.Health}}'
curl -sS http://127.0.0.1:8088/v1/models
```

LLM debug (health, `:8088`, Core grep): [`llm.md`](llm.md) §8.

Same via the CLI:

```bash
./forgesre status
./forgesre logs core
./forgesre logs snmp-exporter
./forgesre logs llm
```

**Rebuild Core after a git pull that changed Python** (without reinstalling):

```bash
docker compose build core
docker compose up -d core
docker compose ps core
```

**Inspect the Core container** (working directory, imports):

```bash
docker compose exec -T core pwd
docker compose exec -T core ls
docker compose exec -T core python -c "import sys; print('\n'.join(sys.path))"
```

**Read live YAML on disk:**

```bash
./forgesre config
# or:  less config/forgesre.yml
```

Do not commit `config/forgesre.yml`, `.env`, or `secrets/`. Commit only `config/forgesre.example.yml` if you are changing the template.

---

## 14. Updates

Existing VM after new commits land on `main`:

```bash
git checkout main
git pull origin main
./forgesre update
./forgesre test
./forgesre secrets-check
./forgesre snmp
```

`./forgesre update` = doctor (warn ok) → backup → render-monitoring → compose pull/up (including **snmp-exporter** on `127.0.0.1:9116`) → doctor. It does **not** regenerate passwords. Host backup dumps Postgres via `docker compose exec postgres` (no sqlalchemy on the host). A backup failure is a clear error; update still starts the stack.

Platform backup files: `data/backups/backup_YYYYMMDDTHHMMSSZ/forgesre.tar.gz` (one folder per run). Restore is not silent — `./forgesre restore FOLDER` prints the plan; add `--yes` after `docker compose stop core`, then `./forgesre update`. Administration can create/import the same archives (admin session only). Details: [`operator-handbook.md`](operator-handbook.md) (Platform backup).

---

## 15. Optional local LLM

ForgeRCA (Python) always runs. The local model only **rewrites prose**. Full implementation guide: [`llm.md`](llm.md) (hardware, `fetch-llm`, offline GGUF, external `/v1` server, jobs, debug CLI).

Not stored in git. `./forgesre fetch-llm` pulls Qwen2.5-14B-Instruct Q4_K_M (~9 GB) into `$FORGESRE_DATA/models/model.gguf`, sets `COMPOSE_PROFILES=ai`, and starts llama.cpp on `127.0.0.1:8088`.

```bash
./forgesre fetch-llm
docker compose --profile ai up -d llm
curl -fsS http://127.0.0.1:8088/v1/models
./forgesre doctor          # llm: ok when llama.cpp answers :8088
./forgesre test
```

Need 16 GB RAM for the default 14B GGUF, or ~8 GB if you wget Qwen3-4B into `data/models/model.gguf` (see [`llm.md`](llm.md) §3.C). Without a model, ForgeRCA still runs. Cloud LLMs are not required. Do not re-run `./install.sh` just to add AI.

---

## 16. Troubleshooting

| Symptom | What to do |
|---|---|
| Preflight: port in use | `./install.sh --port 8081` or free 8080 |
| Grafana 3000 busy | Change `GRAFANA_PORT` in `.env`, recreate Grafana |
| `docker info` denied | `usermod -aG docker "$USER"` then re-login, or use sudo |
| Clone URL fails | Use `https://github.com/nsimic2022/forgesre.git` (slash after `.com`) |
| UI only on the VM | You used `127.0.0.1` from the laptop, or 8080 is blocked |
| Doctor cannot fetch | `./forgesre secrets-check` — doctor uses the webhook token |
| Core will not start | Shipped default `SECRET_KEY` / token. Put real values in secrets |
| SNMP empty `[]` | No Network device with an IP yet |
| Doctor snmp paused | No Network device + IP yet — yellow, not DOWN. Overall stays healthy for snmp. |
| Doctor snmp error | `docker compose up -d snmp-exporter && ./forgesre snmp` |
| LLM download / :8088 down | `docker compose --profile ai up -d llm` then wait for the GGUF to load |
| Re-install wiped logins | `./install.sh` regenerates secrets; use `update` on a live box |

```bash
docker compose logs --tail 80 core
./forgesre test
./forgesre doctor
```
