# Docs

The GitHub README is the product summary (what ForgeSRE is, what it is not, first install, **optional LLM** download into `data/models/model.gguf`). This folder is how you **learn to operate** the appliance.

**Why / when:** [operator handbook](operator-handbook.md) (source of truth for running the box).  
**Commands:** [cli.md](cli.md).  
**New Ubuntu VM:** [install-config.md](install-config.md) (`.env` at repo root = deployment; `secrets/secrets.env` = plaintext secrets; UI passwords = bcrypt in Postgres).  
**Existing VM:** `git pull origin main && ./forgesre update` — never `./install.sh` again.

---

## Learn the platform (this order)

1. **Install or update** — New VM: [install and config](install-config.md) then `./install.sh`. Live box: `git pull origin main && ./forgesre update`. Optional local LLM (not required): `./forgesre fetch-llm` downloads into `data/models/model.gguf`, then profile `ai` + `ai.enabled: true` — [llm.md](llm.md).
2. **Login** — `http://<VM-IP>:8080`. Credentials: `installation-report.md` and `secrets/secrets.env`. Handbook [§5](operator-handbook.md#5-users-and-admins).
3. **System Health** — UI `/health-ui` = `./forgesre doctor`. Grafana lives **only here** (graphs, not the alarm path). NetBox UI up + API 403 is yellow **warn**, not paused. SNMP with no Network device + IP is **paused (no SNMP targets)** (yellow — that is OK).
4. **Assets** — `/assets`. Local inventory is what Prometheus scrapes. Add / Edit / Clone / Verify. Handbook [§6–7](operator-handbook.md#6-adding-servers-inventory).
5. **Discovery vs NetBox** — `/discovery`. **Scan now** is TCP/SNMP/`/metrics` (not nmap). **Sync NetBox** is a separate **read-only** pull from bundled NetBox `:8001`. Empty NetBox is **yellow No devices** — that is normal. Approve or Ignore; nothing is auto-added. Handbook [§6.C](operator-handbook.md#c-bundled-netbox-read-sync).
6. **Incidents** — `/incidents` is open/firing work. `/history` is the 90-day archive. IDs look like `INC-0134_16.08.2026_09:13`. Handbook [§8](operator-handbook.md#8-alerts-become-incidents) and [§12](operator-handbook.md#12-incident-workflow).
7. **ForgeRCA vs ForgeAI** — Open ForgeRCA on the incident (`/ai/INC-…`). **ForgeRCA** (Python) always runs first. **ForgeAI** only rewrites prose (optional local LLM, default 90s timeout, **one worker thread**). Handbook [§13](operator-handbook.md#13-ai-investigation-forgerca) · [llm.md](llm.md).
8. **verify ≠ doctor ≠ test** — `./forgesre verify` = live inventory path (Assets → Verify). `./forgesre doctor` = Health lights. `./forgesre test` = appliance report → `data/reports/`. [verify.md](verify.md).
9. **Backup** — Administration or `./forgesre backup`. Restore is not silent (`--yes`). Handbook [Platform backup](operator-handbook.md#platform-backup-and-restore).
10. **CLI cheat sheet** — [cli.md](cli.md). On the box: `./forgesre help`.

UI: `http://<VM-IP>:8080`. NetBox UI: `:8001`. Grafana: `:3000` (open from System Health).

---

## Manuals (after the path above)

- [Operator handbook](operator-handbook.md) — users, assets, discovery, incidents, email, RCA
- [Install and config (Ubuntu / vCenter)](install-config.md)
- [Verify the appliance](verify.md)
- [Operator CLI](cli.md)
- [Local LLM (ForgeAI / llama.cpp)](llm.md)

What each release shipped (optional “why it exists”, not required to operate):

- [V0.1](v0.1.md) · [V0.2](v0.2.md) · [V0.3](v0.3.md) · [V0.4](v0.4.md) · [V0.5](v0.5.md) · [V0.6](v0.6.md) · [V0.7](v0.7.md)

## For developers (not used in production)

These are design notes. You do **not** install or enable them on the VM.

- [Architecture proposal (not the V0.7 appliance runtime)](architecture.md)
- [V0.3 implementation plan](V03_IMPLEMENTATION_PLAN.md)

**Session handoff** (next coding agent / contributor, not an operator start page): [continuation.md](continuation.md).
