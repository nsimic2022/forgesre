# Settings

One place for what an Admin can see and change. Backend: `backend/app/system_settings.py`.
UI: Settings page (new UI, Phase 2). Shell: `./forgesre settings`. API: `/api/v1/settings`.

## Where each value lives

| Kind | File | Who writes it | How it applies |
|---|---|---|---|
| Live settings | `config/forgesre.yml` | UI, API, `./forgesre settings set` | Validated, written, reloaded at once |
| Secrets (tokens, passwords) | `secrets/secrets.env` | `./forgesre settings set-secret KEY` on the host | Core restarts to read them |
| Addresses and ports | `.env`, `docker-compose.yml` | `./install.sh`, or edit `.env` | Restart the stack |

Core mounts `secrets/secrets.env` read-only, so the UI and API never show or write secret
values: they only say whether each one is set. Every change goes to the audit log with the
old and new value (never a secret).

Writing the YAML rewrites the file without its comments; `config/forgesre.example.yml` keeps
the commented reference.

## Groups

General · Network and services · Integrations (GLPI, Zabbix, LibreNMS, NetBox) · Discovery ·
Notification channels (SMTP) · AI (ForgeRCA / ForgeAI). The full list with types and limits
is `REGISTRY` in `system_settings.py`.

## Commands

```bash
./forgesre settings                          # everything, by group
./forgesre settings integrations             # one group
./forgesre settings get ai.enabled
./forgesre settings set ai.llm.timeout_seconds 120
./forgesre settings test                     # every service and integration
./forgesre settings test zabbix
./forgesre settings set-secret ZABBIX_API_TOKEN   # prompts, writes secrets.env, restarts Core
```

## API (Admin; tests need Engineer)

- `GET /api/v1/settings[?group=ai]`
- `PUT /api/v1/settings/{key}` with `{"value": ...}`; 400 with the reason when rejected
- `POST /api/v1/settings/test/{component}`: runs that System Health check now
