# Docs

The GitHub README is the product summary (what ForgeSRE monitors, first install, **optional LLM** download into `data/models/model.gguf`). This folder is how you **operate** the appliance.

**Why / when:** [operator handbook](operator-handbook.md) (source of truth for running the box).  
**Commands:** [cli.md](cli.md).  
**New Ubuntu VM:** [install-config.md](install-config.md) (`.env` at repo root = deployment; `secrets/secrets.env` = plaintext secrets; UI passwords = bcrypt in Postgres).  
**Existing VM:** `git pull origin main && ./forgesre update`.

---

## Learn the platform (this order)

1. **Install or update** — New VM: [install and config](install-config.md) then `./install.sh`. Live box: `git pull origin main && ./forgesre update`. Optional local LLM: `./forgesre fetch-llm` downloads into `data/models/model.gguf`, then profile `ai` + `ai.enabled: true` — [llm.md](llm.md).
2. **Login** — `http://<VM-IP>:8080`. Credentials: `installation-report.md` and `secrets/secrets.env`. Handbook [§5](operator-handbook.md#5-users-and-admins).
3. **System Health** — UI `/health-ui` = `./forgesre doctor`. Grafana (`:3000`) opens from this page. NetBox UI up + API 403 is yellow **warn**. SNMP with no Network device + IP is **paused (no SNMP targets)** (yellow — idle until you add a target).
4. **Assets** — `/assets`. Local inventory is what Prometheus scrapes. Add / Edit / Clone / Verify. Handbook [§6–7](operator-handbook.md#6-adding-servers-inventory).
5. **Discovery and inventory imports** — `/discovery`. Prefills this VM’s primary IPv4 `/24`; **Confirm & scan** writes `discovery.cidrs` and enqueues Postgres `discovery_scan` (one Core worker; empty cidrs skip the probe). Candidate table: open ports, node_exporter, windows_exporter, SNMP. Four cubes: **Scan now** · **NetBox** · **Zabbix** · **GLPI**. Below Add asset, **SNMP devices** Identify talks to bundled LibreNMS (`:8000`) then writes snmp_exporter `forgesre_onboard`. Empty NetBox is **yellow No devices**. Approve or Ignore; manual Assets stay the monitoring source of truth. Handbook [§6.B](operator-handbook.md#b-discovery-scan-the-management-network) / [§6.C](operator-handbook.md#c-bundled-netbox-read-sync). Optional GLPI: [glpi.md](glpi.md).

6. **Incidents** — `/incidents` is the incident list. It defaults to **All** statuses, newest first (filter **Active (not resolved)** or **Not acknowledged** for live work). Columns include Ack and Resolved by. A resolved alert sets `RESOLVED`; the same alert firing again opens a new incident. Closed rows stay on this list. `/history` redirects here. IDs look like `INC-0134_16.08.2026_09:13`. Handbook [§8](operator-handbook.md#8-alerts-become-incidents) and [§12](operator-handbook.md#12-incident-workflow).
7. **ForgeRCA and ForgeAI** — Open ForgeRCA on the incident (`/ai/INC-…`). **ForgeRCA** (Python) always runs first. **ForgeAI** rewrites prose (optional local LLM, default 90s timeout, **one worker thread**). `/forgeai` is the digest home (setup, ask, filters, jobs, reports, cron). Dashboard `#forgeai` is the short teaser. Handbook [§13](operator-handbook.md#13-ai-investigation-forgerca) · [llm.md](llm.md).
8. **verify, doctor, test** — `./forgesre verify` = live inventory path (Assets → Verify). `./forgesre doctor` = Health lights. `./forgesre test` = appliance report → `data/reports/`. [verify.md](verify.md).
9. **Backup** — Administration or `./forgesre backup`. Restore asks for `--yes`. Handbook [Platform backup](operator-handbook.md#platform-backup-and-restore).
10. **CLI cheat sheet** — [cli.md](cli.md). On the box: `./forgesre help`.

UI: `http://<VM-IP>:8080`. NetBox UI: `:8001`. LibreNMS UI: `:8000`. Grafana: `:3000` (open from System Health).

---

## Manuals (after the path above)

- [Operator handbook](operator-handbook.md) — users, assets, discovery, incidents, email, RCA
- [Architecture](architecture.md) — how the V0.9 appliance is put together (Compose, one Core, alarm path)
- [Connecting Zabbix (optional, read-only)](zabbix.md) — operator cookbook, both machines: secrets, Sync hosts, Zabbix user/token, hosts + templates, webhook media type + Action, test (401 vs 422), playrules by Alertname, Prometheus graphs vs `trend.get`, mute vs real triggers, ack/resolve, CLI, numbered checklist (summary: [handbook §18](operator-handbook.md#18-zabbix-read-only-source))
- [Connecting GLPI (optional, read-only inventory)](glpi.md) — API client + user token, secrets, Discovery → Sync hosts, match by GLPI id / IP / hostname, no write-back (summary: [handbook §19](operator-handbook.md#19-glpi-read-only-source))
- [Install and config (Ubuntu / vCenter)](install-config.md)
- [Verify the appliance](verify.md)
- [Operator CLI](cli.md)
- [Local LLM (ForgeAI / llama.cpp)](llm.md)

What each release shipped:

- [V0.1](v0.1.md) · [V0.2](v0.2.md) · [V0.3](v0.3.md) · [V0.4](v0.4.md) · [V0.5](v0.5.md) · [V0.6](v0.6.md) · [V0.7](v0.7.md) · [V0.8](v0.8.md) · [V0.9](v0.9.md) (current)

## For developers (not used in production)

[V0.3 implementation plan](V03_IMPLEMENTATION_PLAN.md) is a design note from that slice. [Architecture](architecture.md) is the running V0.9 appliance.

**Session handoff** (next coding agent / contributor): [continuation.md](continuation.md).
