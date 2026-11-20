# What is still only on the classic UI

The new UI (`/app`) is home since V2.0; the classic pages (`/classic`, `/assets`, `/ops` …) still
work. This list is what an operator can do **only** on the classic pages today, so the decision to
remove them can be made item by item. Nothing here is removed until that decision is made.

| Classic page | In the new UI | Only in classic | Proposal |
|---|---|---|---|
| Dashboard `/classic` | NOC overview, TV | the old tile layout | drop with classic |
| Assets `/assets`, `/assets/{id}` | Inventory, asset card, Add devices, monitoring check, CSV import, Sources, capacity | multi-select **bulk delete** and **export of the inventory (TXT / CSV)**; **Verify all assets** in one run | port: bulk delete + CSV export (small); Verify all stays `./forgesre verify` |
| Discovery `/discovery`, `/discovery/snmp`, `/discovery/scan-ports` | Inventory › Add devices (identify, scan a network, add), Sources | the saved list of found devices with Approve / Ignore across scans, SNMP walk page, port-scan detail | port the found-devices list (needed for `inventory.new_from_sources: review`); drop the scan-port page |
| Incidents `/incidents`, `/history` | Incidents list (filters, dates, CSV), incident page | bulk edit / delete of incidents, audit export of one incident (TXT / CSV) | port the audit export; bulk delete stays CLI / classic (rarely needed) |
| Incident report `/incidents/{n}` | incident page with Send report, Postmortem, ForgeAI | report export as TXT / CSV | drop: Postmortem Markdown and the mail cover it |
| ForgeAI `/forgeai`, `/ai/{n}` | incident ForgeAI card, Investigations, ForgeAI skills with tuning, ForgeAI digest mail | the old insights dashboard (repeats, hot groups, parent–child, aging) with its own cron | drop: the digest mail and Investigations replace it |
| Playrules, Playbooks, Escalation `/playrules`, `/playbooks`, `/escalation` | Automation (rules, playbooks, policies, noise filters, try an alarm), On call | export of rules / playbooks / on-call (TXT / CSV) | port one "Export" (JSON) for rules, playbooks and policies, useful for review and backup |
| Ops `/ops` | Reports (scheduled reports, outbox), Send report on incidents | **free-form mail** to anyone with the address book, outbox bulk delete / export | port free-form mail only if it is still used; outbox export → CSV on Reports |
| Journal `/journal` | Settings › Journal | bulk delete, export | drop bulk delete; keep export via `./forgesre journal` |
| Health `/health-ui` | Settings › Services and health, `./forgesre doctor` | — | drop |
| Admin `/admin` | Settings › Users and roles, Backup, Demo, Audit log | **restore a backup** (confirmed step), audit export, the CLI command list | port restore with a typed confirmation; the CLI list moves to docs/cli.md only |

## Before classic can go

1. ~~Port the four small gaps~~ (done): Inventory **Remove selected** and **Export CSV** (the
   columns Import CSV reads), the found-devices list in Add devices with where each came from,
   **Restore** on Settings › Backup (warning ticked, RESTORE typed, runs in the background), and
   **Export (JSON)** on Automation.
2. Point the remaining links (`More (classic)` on the asset card, Restore on Settings › Backup) at
   the new pages.
3. Keep `/classic` one more release behind `ui.home: classic`, then remove the templates, the
   classic routes in `web.py`, `web_*.py`, `bulk.py` and `frontend/`, with their tests.
