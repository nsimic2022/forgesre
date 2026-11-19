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
Notification channels (SMTP, Slack, Microsoft Teams, Discord) · AI (ForgeRCA / ForgeAI). The full list with types and limits
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

## Chat channels: Slack, Microsoft Teams, Discord

Chat is an extra channel next to email. When a channel is on, ForgeSRE posts one line when
an incident opens, at each escalation step and when it resolves, for incidents at or above
the channel's minimum severity (default `high`).

Chat messages leave the server. They carry the severity, title, host and incident number
(and a link when `system.public_url` is set), never logs, evidence or ForgeAI text.

1. Create an incoming webhook:
   - Slack: an app with *Incoming Webhooks*, one channel.
   - Teams: a Workflows flow *When a Teams webhook request is received* that posts to a
     channel (it receives an Adaptive Card).
   - Discord: channel settings › Integrations › Webhooks.
2. Store the URL on the host. Only `https://` URLs are accepted:
   ```bash
   ./forgesre settings set-secret SLACK_WEBHOOK_URL     # or TEAMS_WEBHOOK_URL, DISCORD_WEBHOOK_URL
   ```
3. Turn it on and send a test message:
   ```bash
   ./forgesre settings set notifications.chat.slack.enabled true
   ./forgesre settings set notifications.chat.slack.min_severity critical   # optional
   ./forgesre settings test slack
   ```

Messages are sent by the background jobs worker with a 10-second timeout, so a slow chat
service never holds up alerts or the UI. Each message is a row in the incident's
notifications (channel `slack`, `teams` or `discord`) with `sent` or `failed` and the reason.
`./forgesre doctor` shows a **Chat** row: off, OK, or the last delivery error.

## API (Admin; tests need Engineer)

- `GET /api/v1/settings[?group=ai]`
- `PUT /api/v1/settings/{key}` with `{"value": ...}`; 400 with the reason when rejected
- `POST /api/v1/settings/test/{component}`: runs that System Health check now
