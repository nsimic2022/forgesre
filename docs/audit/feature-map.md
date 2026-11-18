# Phase 0 — feature map (V0.9, November 2026)

What the UI has today, page by page, as the starting point for the ForgeSRE 2 redesign.
Counts come from a signed-in admin walk of a seeded lab (`tests/test_gui_crawl.py`).

## How to check a live appliance

```bash
python3 scripts/gui_crawl.py --base-url http://127.0.0.1:8080 \
    --email admin@forgesre.local --password '<from secrets/secrets.env>'
```

It signs in, follows every link (GET only, never submits a form) and lists pages that
answer 5xx, links that answer 404, and broken scripts or stylesheets. Exit code 1 when
anything is broken. The unit-test walk (`tests/test_gui_crawl.py`) also checks that
every form posts to a route that exists; it runs in CI.

Seeded lab result: 26 pages, **no 5xx, no 404 links, no forms without a route**.
What breaks on a real appliance (Prometheus, SNMP, Zabbix, mail, LLM) only shows there,
so the live walk plus `./forgesre doctor` is the real Phase 0 check.

## Pages today

| Menu | Path | Sections on the page | Forms / buttons |
|---|---|---|---|
| Dashboard | `/` | Run demo, Incidents, Infrastructure, Recent investigations, Recent incidents, Host metrics | 11 / 17 |
| Investigations | `/investigations` (+ `/forgeai`) | case list | 1 / 3 |
| Assets | `/assets`, `/assets/{id}`, `/assets/verify` | inventory table (13 columns), asset detail, verify | 8 / 12 |
| Discovery | `/discovery` (+ `/discovery/snmp`, `/discovery/scan-ports`) | Scan now, LibreNMS, NetBox sync, Zabbix, GLPI, Found assets, Add asset (Identity, Ports, Metrics, Contacts) | 10 / **47** |
| Incidents | `/incidents`, `/incidents/{number}` | list (8 columns), detail | 6 / 10 |
| Playrules | `/playrules` | rules table, Create rule | 16 / 20 |
| Playbooks | `/playbooks` | one card per playbook, Create playbook | 6 / 10 |
| Escalation | `/escalation` | Default escalation, Create policy, Add on-call, On call | 10 / 16 |
| Email & reports | `/ops` | Send through, Add email, Compose, Mail outbox, Scheduled reports, New report, Send now | 13 / 25 |
| System Health | `/health-ui` (+ `/journal`) | component table (State, Why, Test, Fix, Open), ForgeSRE metrics, Journal | 6 / 10 |
| Administration | `/admin` | Users, Create user, Audit log, History backup, Create backup | 6 / 10 |

Pages reachable only by typing the URL: `/assets/verify`, `/discovery/snmp`,
`/discovery/scan-ports`, `/forgeai`, `/history` and `/journal` (the last two redirect).

## Early observations

- **Discovery** carries five integrations, a scanner and the add-asset form on one page
  (47 buttons). The integration settings belong on a Settings page; Discovery should
  only find hosts and approve them.
- **Playrules, Playbooks and Escalation** are three menu items for one chain
  (alert → checklist → who is called). One "Automation" area with three tabs reads better.
- **Email & reports** mixes SMTP settings, an outbox and scheduled reports. SMTP goes to
  Settings; reports get their own page.
- There is no page for system settings (IP addresses, service URLs, tokens, SMTP, LLM):
  today they live in `secrets/secrets.env`, `.env` and `config/forgesre.yml`.

Decisions per page (keep / fix / drop / move) are recorded in the redesign discussion.
