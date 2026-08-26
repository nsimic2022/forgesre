# ForgeSRE operator CLI

All commands run **on the VM**, from the clone directory (`~/forgesre`). ForgeSRE does not speak SSH of its own — you SSH to Ubuntu, then use this CLI on localhost.

```bash
ssh you@forgesre-vm
cd ~/forgesre
./forgesre help
```

`./f` is the same binary. `./forgesre` with no arguments opens a prompt (`forgesre>`). Type `journal`, `incidents`, `doctor` — not `./forgesre` again. Leave with `quit`, `exit`, or Ctrl-D (`./forgesre help quit`). TAB completes command names, Compose services (`logs sn<TAB>`), incident ids, and asset numbers/ids/hostnames (`verify 1<TAB>`).

Two logins:

1. **Linux SSH** — OS account on the VM.
2. **ForgeSRE user** — Administration → users. `./forgesre login` stores `data/cli.session`. Without it, the CLI uses the install admin from `secrets/secrets.env` when that file is readable.

---

## Everyday commands

```bash
./forgesre help
./forgesre help quit
./forgesre help test
./forgesre help snmp
./forgesre                 # prompt (forgesre>); leave with quit
./forgesre doctor          # short lights
./forgesre ping            # ICMP + exporter /metrics (alias: probe)
./forgesre ping win10-gp
./forgesre verify          # live chain: exporter → prometheus → alertmanager → core (not test)
./forgesre verify 12                 # asset number #
./forgesre verify win10-gp           # Asset ID
./forgesre verify DESKTOP-CG81N3J    # hostname
./forgesre verify 10.10.10.60        # IP
./forgesre test            # detailed report → data/reports/
./forgesre status          # docker compose ps
./forgesre logs core
./forgesre config
./forgesre assets
./forgesre snmp
./forgesre sd
./forgesre incidents
./forgesre history --days 90
./forgesre jobs
./forgesre journal
./forgesre journal snmp
./forgesre demo
./forgesre demo-reset
./forgesre secrets-check
./forgesre render-monitoring
./forgesre backup
./forgesre backup --no-secrets
./forgesre backup --include-models
./forgesre restore                 # numbered backup_* picker (newest first); still needs --yes
./forgesre restore data/backups/backup_YYYYMMDDTHHMMSSZ --yes
./forgesre import backup           # same picker as restore
./forgesre remove backup           # numbered picker; delete that folder only with --yes
./forgesre mailbox         # optional; does not rewrite Gmail/Outlook SMTP
./forgesre fetch-llm
./forgesre update
./forgesre version
./forgesre login
./forgesre whoami
./forgesre logout
```

Root wrappers still work: `./install.sh`, `./doctor.sh`, `./test.sh`, `./backup.sh`, `./restore.sh`, `./update.sh`.

### Leave the prompt

```bash
./forgesre                 # opens forgesre>
journal
incidents
quit                       # or: exit    or Ctrl-D
./forgesre help quit
```

`quit` is a prompt command. From host bash you are already out; `./forgesre quit` only prints the same help.

---

## Ping vs scrape

ICMP ping from the appliance only proves **L3** (the host answers ping). ForgeSRE **sees** a host when Prometheus scrapes exporter `/metrics`. `./forgesre ping` (alias `./forgesre probe`) checks both from this VM, using inventory already in ForgeSRE — no extra flags for the common case.

```bash
./forgesre ping
./forgesre ping 12
./forgesre ping win10-gp
./forgesre ping DESKTOP-CG81N3J
./forgesre ping 10.10.10.60
./forgesre probe              # same command
./forgesre help ping
```

| ICMP | METRICS | Meaning |
|---|---|---|
| PASS | PASS | Host is up and the exporter answers. Prometheus can scrape it (wait ~30s, then `./forgesre sd`). |
| PASS | FAIL | Host is on the network; ForgeSRE still cannot see it. Windows: `windows_exporter` not running, firewall **TCP 9182**, or scrape port is Linux **:9100**. Linux: `node_exporter` / **TCP 9100**. |
| FAIL | FAIL | Wrong IP, host down, or ICMP and the exporter port both blocked. |
| PASS | SKIP | Network device — HTTP metrics do not apply. Use `./forgesre snmp` (UDP/161). |

Linux default scrape is `:9100`. Windows Server default is `:9182`. Configured `scrape_address` wins. An ad-hoc IP (no inventory type) probes **both** ports and classifies `windows_` vs `node_` the same way Assets/Discovery detect does (both → prefer Windows `:9182` unless a saved type exists). Seeded `forge-demo-*` rows and discovery seed `10.20.30.41` are skipped unless you pass `--demo` or the id.

---

## Verify (live communication)

`./forgesre test` is appliance health (files, Compose, login, APIs) after `update`. **`./forgesre verify` is a different command**: live communication for inventory already in ForgeSRE. It runs on the **host** CLI (Ubuntu Python has no sqlalchemy — do not pip-install it). GUI Verify still runs inside Core.

```bash
./forgesre verify
./forgesre verify 12
./forgesre verify win10-gp
./forgesre verify DESKTOP-CG81N3J
./forgesre verify 10.10.10.60
./forgesre verify --demo
./forgesre help verify
```

Path: inventory row → ICMP / exporter (`:9100` `node_` or `:9182` `windows_`) or SNMP UDP/161 → Prometheus `up` + scrape target health + family series → Alertmanager reachable → last Core incident/webhook (SKIP if none). ForgeAI/LLM is listed only when enabled; verify does **not** call the LLM. Chain: `exporter → prometheus → alertmanager → core`.

Classes are universal, not SKUs: Linux, Windows, Network SNMP, Unknown. Unknown or a missing exporter / no Prom target is **SKIP or FAIL with an honest reason** — never a fake green host. Seeded `forge-demo-*` rows and the discovery Approve seed `10.20.30.41` (`disc-10-20-30-41`) are **lab** (label DEMO), are not in HTTP SD, and are not proof of a real scrape. Verify does **not** call the LLM even when ForgeAI is enabled.

`verify` accepts **all of**: asset number `#`, Asset ID, hostname, and IP. Same keys work for `./forgesre ping`. TAB completes numbers and ids (hostnames too). One key dumps what ForgeSRE already knows (inventory) plus the live checks. Same action: Assets → **Verify** (analyst / engineer / admin). Viewers are read-only.

---

## Advanced CLI

Use on a live box. **Never** `./install.sh` again — that regenerates passwords.

### Git pull (existing VM)

```bash
git checkout main
git pull origin main
./forgesre update
./forgesre test
./forgesre snmp
```

`./forgesre update` = doctor (warn ok) → backup → render-monitoring → `docker compose up -d` (includes **snmp-exporter** on `127.0.0.1:9116`) → doctor.

Backup on the host does **not** use sqlalchemy (that package is only in the Core image). The CLI dumps Postgres with `docker compose exec postgres` using the **same docker rights as update** (`docker info`, otherwise `sudo docker compose`). Administration Backup still runs inside Core. Each run is `data/backups/backup_YYYYMMDDTHHMMSSZ/forgesre.tar.gz` (plus `MANIFEST.txt`); import that one tar, do not unpack it. `./forgesre backup` still exists; `./forgesre update` also runs backup as a safety net. If backup fails, update prints a clear error and **continues** so the stack still comes up. A `docker.sock` permission error is not “postgres is down”. Do not `pip install sqlalchemy` on the host. Do not `./install.sh`.

### Rebuild Core after Python changes

```bash
docker compose build core
docker compose up -d core
docker compose ps core
```

### Logs (Core, RCA, LLM)

```bash
docker compose logs --tail=100 core
docker compose logs --tail=100 core | grep -iE "llm|rca|error|exception"
docker compose logs --tail=50 core | grep "/ai"
docker compose logs -f core
docker compose logs --tail=200 llm
./forgesre logs core
./forgesre logs llm
```

### LLM container

Full implementation guide (including health inspect and `/v1/chat/completions`): [`llm.md`](llm.md) §8.

```bash
./forgesre fetch-llm
# lab 8 GB RAM: wget Qwen3-4B into data/models/model.gguf, then:
./forgesre fetch-llm --offline
docker compose --profile ai up -d llm
docker compose ps llm
docker compose ps -q llm | xargs -r docker inspect --format='{{json .State.Health}}'
curl -sS http://127.0.0.1:8088/v1/models
curl -sS http://127.0.0.1:8088/health
docker compose logs --tail=100 llm
docker compose logs --tail=100 core | grep -iE "llm|openai|model|error|exception"
docker compose logs -f llm
./forgesre logs llm
./forgesre doctor
./forgesre test
```

Do not `docker compose down` to “fix” LLM — that stops the whole appliance.

### Inside the Core container

```bash
docker compose exec -T core pwd
docker compose exec -T core ls
docker compose exec -T core python -c "import sys; print('\n'.join(sys.path))"
```

### VMware guest tools

```bash
sudo apt install -y open-vm-tools
sudo systemctl enable --now open-vm-tools
systemctl status vmtoolsd
```

`config/forgesre.yml` is local to the VM (gitignored). Change it, then recreate Core. Do not commit it. The committed template is `config/forgesre.example.yml`.

---

## API (session cookie)

After `./forgesre login` or a UI login cookie:

| Method | Path | Who |
|---|---|---|
| POST | `/api/v1/users` | admin (create) |
| POST | `/api/v1/users/{id}` | admin (edit) |
| POST | `/api/v1/users/{id}/delete` | admin |
| GET | `/api/v1/assets` | viewer+ |
| POST | `/api/v1/assets` | analyst+ |
| GET | `/api/v1/assets/{id}/verify` | analyst+ (live path; same as `./forgesre verify <id>`) |
| GET | `/api/v1/assets/{id}/metrics` | viewer+ (class tiles: CPU/mem/disk/up from Prometheus) |
| GET | `/api/v1/verify` | analyst+ (all real assets; `?include_demo=1` labels DEMO) |
| POST | `/api/v1/assets/{id}` | analyst+ (edit) |
| POST | `/api/v1/assets/{id}/clone` | analyst+ |
| POST | `/api/v1/assets/{id}/delete` | analyst+ |
| GET | `/api/v1/history` | viewer+ |
| GET | `/api/v1/system/doctor` | login or Bearer webhook token |
| GET | `/api/v1/sd/prometheus` | Bearer webhook token |
| GET | `/api/v1/sd/snmp` | Bearer webhook token |
| POST | `/api/v1/webhooks/alertmanager` | Bearer webhook token |

Install and file layout: [`install-config.md`](install-config.md). Verification report: [`verify.md`](verify.md). Local LLM: [`llm.md`](llm.md).
