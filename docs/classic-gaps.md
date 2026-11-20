# How the classic UI was retired

The classic server-rendered pages (`/classic`, `/assets`, `/ops` …) were removed in V2.0. The
new UI (`/app`) is the only UI. Old addresses redirect to the matching `/app` page
(`backend/app/web.py`, `REDIRECTS`), so bookmarks and links in old mails keep working. Core still
renders two pages itself: sign-in (`/login`) and the mail acknowledge link (`/ack/…`).

What each classic page had, and where it went:

| Classic page | In the new UI | Only in classic | Proposal |
|---|---|---|---|
| Dashboard `/classic` | NOC overview, TV | the old tile layout | drop with classic |
| Assets `/assets`, `/assets/{id}` | Inventory, asset card, Add devices, monitoring check, CSV import, Sources, capacity | multi-select **bulk delete** and **export of the inventory (TXT / CSV)**; **Verify all assets** in one run | port: bulk delete + CSV export (small); Verify all stays `./forgesre verify` |
| Discovery `/discovery`, `/discovery/snmp`, `/discovery/scan-ports` | Inventory › Add devices (identify, from LibreNMS, add), Sources | the saved list of found devices with Approve / Ignore across scans, SNMP walk page, port-scan detail | port the found-devices list (needed for `inventory.new_from_sources: review`); drop the scan-port page |
| Incidents `/incidents`, `/history` | Incidents list (filters, dates, CSV), incident page | bulk edit / delete of incidents, audit export of one incident (TXT / CSV) | port the audit export; bulk delete stays CLI / classic (rarely needed) |
| Incident report `/incidents/{n}` | incident page with Send report, Postmortem, ForgeAI | report export as TXT / CSV | drop: Postmortem Markdown and the mail cover it |
| ForgeAI `/forgeai`, `/ai/{n}` | incident ForgeAI card, Investigations, ForgeAI skills with tuning, ForgeAI digest mail | the old insights dashboard (repeats, hot groups, parent–child, aging) with its own cron | drop: the digest mail and Investigations replace it |
| Playrules, Playbooks, Escalation `/playrules`, `/playbooks`, `/escalation` | Automation (rules, playbooks, policies, noise filters, try an alarm), On call | export of rules / playbooks / on-call (TXT / CSV) | port one "Export" (JSON) for rules, playbooks and policies, useful for review and backup |
| Ops `/ops` | Reports (scheduled reports, outbox), Send report on incidents | **free-form mail** to anyone with the address book, outbox bulk delete / export | port free-form mail only if it is still used; outbox export → CSV on Reports |
| Journal `/journal` | Settings › Journal | bulk delete, export | drop bulk delete; keep export via `./forgesre journal` |
| Health `/health-ui` | Settings › Services and health, `./forgesre doctor` | — | drop |
| Admin `/admin` | Settings › Users and roles, Backup, Demo, Audit log | **restore a backup** (confirmed step), audit export, the CLI command list | port restore with a typed confirmation; the CLI list moves to docs/cli.md only |

## What was done

1. Ported: Inventory **Remove selected** and **Export CSV** (the columns Import CSV reads), the
   found-devices list in Add devices with where each came from, **Restore** on Settings › Backup
   (warning ticked, RESTORE typed, runs in the background), and **Export (JSON)** on Automation.
2. Removed: the templates except sign-in and acknowledge, `frontend/static`, the classic routes
   in `web.py`, `web_assets.py`, `web_discovery.py`, `web_incidents.py`, `bulk.py`,
   `insights.py`, the `ui.home` setting, and their tests. `scripts/gui_crawl.py` now walks the
   `/app` pages and every `/api/v1` read instead.
