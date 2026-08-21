# ForgeSRE operator CLI

All commands run **on the VM**, from the clone directory (`~/forgesre`). ForgeSRE does not speak SSH of its own — you SSH to Ubuntu, then use this CLI on localhost.

```bash
ssh you@forgesre-vm
cd ~/forgesre
./forgesre help
```

`./f` is the same binary. `./forgesre` with no arguments opens a prompt (`forgesre>`). Type `journal`, `incidents`, `doctor` — not `./forgesre` again. Leave with `quit`, `exit`, or Ctrl-D (`./forgesre help quit`). TAB completes command names, Compose services (`logs sn<TAB>`), and incident ids.

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
./forgesre mailbox         # optional; does not rewrite Gmail/Outlook SMTP
./forgesre fetch-llm
./forgesre update
./forgesre version
./forgesre login
./forgesre whoami
./forgesre logout
```

Root wrappers still work: `./install.sh`, `./doctor.sh`, `./test.sh`, `./backup.sh`, `./update.sh`.

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

## Advanced CLI

Use on a live box. **Never** `./install.sh` again — that regenerates passwords.

### Git pull (existing VM)

```bash
git checkout main
git pull origin main
./forgesre update
./forgesre test
```

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
docker compose --profile ai up -d llm
docker compose ps llm
docker compose ps -q llm | xargs -r docker inspect --format='{{json .State.Health}}'
curl -sS http://127.0.0.1:8088/v1/models
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
| GET | `/api/v1/history` | viewer+ |
| GET | `/api/v1/system/doctor` | login or Bearer webhook token |
| GET | `/api/v1/sd/prometheus` | Bearer webhook token |
| GET | `/api/v1/sd/snmp` | Bearer webhook token |
| POST | `/api/v1/webhooks/alertmanager` | Bearer webhook token |

Install and file layout: [`install-config.md`](install-config.md). Verification report: [`verify.md`](verify.md). Local LLM: [`llm.md`](llm.md).
