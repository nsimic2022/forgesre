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

General · Checks · Alarms (storms, flapping) · Network · Integrations (GLPI, Zabbix, LibreNMS, NetBox, MCP) ·
Discovery · Notifications (SMTP, Slack, Microsoft Teams, Discord) · RCA / AI · Sign-in. The page adds
Messages and templates, Services and health, Users and roles, and Backup. The full list with types and limits
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

Chat is an extra channel next to email. When a channel is on, ForgeSRE posts a card when
an incident opens, at each escalation step and when it resolves, for incidents at or above
the channel's minimum severity (default `high`): a colour bar for the severity (green when
resolved), the headline, host, incident number, severity, start time and an **Open incident**
button. Slack gets Block Kit in an attachment, Teams an Adaptive Card, Discord an embed; the
plain line stays as the notification text.

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

## Integrations

**Settings › Integrations** shows one card per source (Zabbix, GLPI, LibreNMS, NetBox,
Kubernetes, MCP) and per part of the monitoring stack (Prometheus, Alertmanager, Loki, Alloy,
SNMP exporter, Grafana, the ForgeAI model). Each card has the doctor status and reason, the last
sync from the journal, **Open** links to the service's own page, **Test connection**, **Sync now**
(Zabbix, GLPI, NetBox, LibreNMS; a background `integration_sync` job) and its settings in place.
Services that listen only on 127.0.0.1 show the SSH tunnel to use instead of a link. Tokens are
still set only on the server with `./forgesre settings set-secret`.
`GET /api/v1/integrations`, `POST /api/v1/integrations/{zabbix|glpi|netbox|librenms}/sync` (Admin).

## Journal and audit log

**Settings › Journal** is the platform's own report (every sync, job, delivery and check with
its result), filterable by module and errors; `GET /api/v1/journal` (Analyst and up), same as
`./forgesre journal`. **Settings › Audit log** lists who changed what (`GET /api/v1/audit`,
Admin). The mail and chat outbox is on Reports (`GET /api/v1/outbox`, Analyst and up).

## Appearance

Six themes: Signal (dark, default), Light, Midnight, Navy (the wall-screen colours: deep blue
panels, yellow and soft blue accents), High contrast and Follow the computer. The choice is kept in
the browser (`localStorage`), so a wall screen and a desk can differ; the TV page uses Navy in a
browser that never chose a theme.
Users who are not Admins find the theme picker on the Settings page too.

## Messages and templates

**Settings › Messages** renders, for any incident (the newest by default, a
sample on an empty appliance), the alert mail, the chat card for each event and channel (with
the exact JSON that is posted) and the incident report. It uses the same builders as a real
send and sends nothing. `GET /api/v1/message-previews[?incident=INC-…]` (any signed-in user).

Scheduled reports have **Preview** (`GET /api/v1/scheduled-reports/{id}/preview`, or
`/scheduled-reports/preview` for a sample) and **Send now** (`POST
/api/v1/scheduled-reports/{id}/send`, Engineer, queues a `report_send` job; the schedule does
not move).

## API (Admin; tests need Engineer)

- `GET /api/v1/settings[?group=ai]`
- `PUT /api/v1/settings/{key}` with `{"value": ...}`; 400 with the reason when rejected
- `POST /api/v1/settings/test/{component}`: runs that System Health check now

Sign-in with LDAP / Active Directory or OIDC single sign-on: [sign-in.md](sign-in.md).
