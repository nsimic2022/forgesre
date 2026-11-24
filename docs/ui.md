# New UI (ForgeSRE 2)

React + TypeScript in `web/`, built by Vite into `web/dist` and served by Core at `/app`.
It is the only UI: `/` sends a signed-in user to `/app/`. The old server-rendered pages are
gone; their addresses (`/incidents`, `/assets`, `/admin`, `/classic`, …) redirect to the
matching `/app` page so bookmarks keep working. Only sign-in (`/login`) and the mail
acknowledge link (`/ack/…`) are still rendered by Core (`frontend/templates`).

## How it runs on the appliance

- `docker compose build core` (and `./forgesre update` when `web/` changes) builds the UI in
  a Node stage of `backend/Dockerfile`; the Core image only carries the built files.
- The browser talks to Core alone. Fonts are bundled (`@fontsource`); no CDN, no analytics,
  nothing leaves the appliance.
- Data comes from `/api/v1/*` with the normal session cookie. A 401 sends the browser to
  `/login`. Each page reads one compact endpoint (NOC: `GET /api/v1/noc`, polled every 10 s).
- Without a built UI (`web/dist` missing), `/` and the old addresses go to `/login`; run `./forgesre update` or `docker compose build core`.

## What changed before an incident

The incident page has a **What changed before** card: everything recorded in the 24 hours
before the alarm on this asset, on its parent, or with no asset (global), newest first, with
how long before the alarm it happened. Two sources are merged:

- **Posted changes**: deploys, config pushes and change tickets. CI/CD, scripts or GLPI
  webhooks post them with the `CHANGES_WEBHOOK_TOKEN` bearer (install generates it; read it
  on the host with `grep CHANGES_WEBHOOK_TOKEN secrets/secrets.env`), or a signed-in Engineer
  posts one:

  ```bash
  curl -sS -X POST https://forgesre.example/api/v1/changes \
    -H "Authorization: Bearer $CHANGES_WEBHOOK_TOKEN" -H 'Content-Type: application/json' \
    -d '{"title":"erp-api 4.2.1","kind":"deploy","source":"gitlab","asset":"db-ora-02","url":"https://ci.example/pipelines/812"}'
  ```

  `kind` is deploy, config, change, ticket, maintenance or other; `asset` is a hostname, asset
  id or IP (unknown names keep the change global and note the name); `at` (ISO time) is
  optional. `GET /api/v1/changes` lists the latest.
- **ForgeSRE's audit log**: edits to the asset, its parent and its maintenance, and any change
  to routing rules, playbooks, escalation, noise filters and settings.

## SLOs and error budget

**Reports › SLOs** (Engineer edits, everyone reads). An SLO is a target availability over a
window, e.g. 99.9 % over 30 days, for all assets, one asset, an asset type or a site, counting
incidents of the chosen severities (critical by default).

- **Availability**: the share of the window without such an open incident. For a group the
  incidents are merged, so two servers down at the same time count once (the service was
  down while any member was).
- **Error budget**: what the target allows (99.9 % of 30 days = 43 minutes), used and left.
  Over 75 % used is *at risk*, 100 % is *spent*.
- **Burn 24 h**: downtime in the last day compared with spending the budget evenly; above
  1.0x the budget runs out before the window ends.
- Maintenance windows and noise-filtered incidents do not burn budget.

Nothing is stored per day: changing an SLO recomputes it from incident history.
`GET /api/v1/slos`, `POST/PUT/DELETE /api/v1/slos[/{id}]`, and `./forgesre slo`.

## Postmortem draft

**Postmortem** on the incident page builds a Markdown draft for the review meeting from the
facts ForgeSRE has: summary table (times to acknowledge and resolve, highest level, rule and
playbook), impact (assets behind it in the dependency map), the review's root cause (or
ForgeRCA's suggestion, marked as not confirmed), what changed before, a timeline of alarm,
escalation steps, notifications, notes, ack and resolve, ForgeRCA's facts, the playbook
checklist, and follow-up actions. Nothing is invented: every gap is `[to fill]`, counted in
the panel. **Copy Markdown** or **Download .md**
(`GET /api/v1/incidents/{number}/postmortem?download=1`), then paste it into your wiki.

## Send report

**Send report** on the incident page (Analyst+) mails the incident report. The box is filled
with the subject (`Alarm <host>`) and the generated report; buttons add the asset owner, earlier
recipients of this incident and the address book. Left as it is, the report goes as text and
formatted HTML; edited, as plain text. The `incident_mail` job sends one mail per address, so
the page never waits on SMTP, and each one is a row in the Outbox (Reports page) with status
sent, failed (with the SMTP error) or generated when SMTP is off.

## Dependency map

**Inventory › Dependency map** (`/app/inventory/map`) shows who sits behind whom. The parent
pointer on each asset is what parent-down suppression uses: while a parent has an open down
incident, alarms from the assets behind it do not mail anyone, so a dead access switch gives
one incident instead of one per server. Those child incidents still exist and open if they
keep failing after the parent is back.

- **Sync from LibreNMS** (Engineer) reads LLDP/CDP neighbours in the background
  (`topology_sync` job) and matches both ends to assets by hostname, sysName or IP.
- **Suggested parents**: only where exactly one end of a link is a network device (Switch,
  Router, Firewall, Network device). The other end gets it as parent. Switch-to-switch links
  are not guessed, because the direction towards the core is not known. Accept one, or all.
- **Tree**: largest first, red for an open alarm, "N behind it", and **Change parent** with a
  loop check.

API: `GET /api/v1/topology`, `POST /api/v1/topology/sync`, `POST /api/v1/topology/parent`
`{asset_id, parent_asset_id}` (empty clears). Writes need Engineer.

## Working many incidents

- Incidents and NOC: tick rows (or the header box for the page) and press **Acknowledge selected**
  or **Resolve selected** (`POST /api/v1/incidents/bulk`, Analyst and up, one audit row each).
- Incident page, **Related and quiet**: **Caused by INC-…** marks this incident as a consequence of
  another when the dependency map did not catch it (loops are refused; the other incident lists
  it under Consequences), and **Quiet this host for 1–24 h** with a reason opens a maintenance
  window from now (Engineer and up). Column `incidents.caused_by_id`, added by the migration.
- Inventory: the asset card shows the host metrics of the last hour.
## Inventory tree

**List | Tree** on Inventory. Tree groups assets by site → row → rack (a missing site is "No
site"; a missing row or rack is skipped), and puts children under their parent: VMs under their
hypervisor, blades under their chassis, wherever the parent stands. Every node shows its worst
state (red = something down inside) and how many open incidents it holds; searching opens the
path to each hit; clicking an asset opens its card. `/app/inventory?view=tree` opens it directly.
Data: `GET /api/v1/inventory/tree`.

## Long lists

Every table and list shows 10 rows at first, with a pager under it: "1–10 of 57", page numbers,
and **Per page** 10, 20 or 50 (remembered per list in this browser). Lists the server pages
(incidents, inventory, investigations, outbox, audit) ask it for one page at a time; the others
page what is already loaded. Code: `web/src/components/Pager.tsx`.

## Keyboard and accessibility

- <kbd>/</kbd> puts the cursor in the page's search box; <kbd>?</kbd> lists the shortcuts.
- <kbd>g</kbd> then a letter opens a page: <kbd>n</kbd> NOC, <kbd>i</kbd> Incidents, <kbd>o</kbd> On-call,
  <kbd>v</kbd> Inventory, <kbd>a</kbd> Automation, <kbd>t</kbd> Investigations, <kbd>r</kbd> Reports,
  <kbd>s</kbd> Settings, <kbd>w</kbd> wall screen. Keys are ignored while you type in a field.
- **Skip to content** is the first stop for the Tab key; the browser tab is named after the page
  ("Inventory · ForgeSRE", "INC-0412 · ForgeSRE"). Code: `web/src/components/Shortcuts.tsx`.

## Command palette, alerts in the browser, phone

- <kbd>Ctrl</kbd>+<kbd>K</kbd> (<kbd>Cmd</kbd>+<kbd>K</kbd> on a Mac) opens **Go to**: type part of a
  page name, an incident number or title, or a host, asset id or IP; arrows move, Enter opens.
  Search is `GET /api/v1/search?q=` (2+ characters, 8 incidents and 8 assets). Code:
  `web/src/components/CommandPalette.tsx`.
- **Settings › Appearance › Alerts in this browser**: *Play a sound* (two short tones made in the
  browser) and *Desktop notification* (the browser asks for permission). Every 30 s the page asks
  `GET /api/v1/notify/critical?after=<id>` for new open CRITICAL incidents; ones suppressed under a
  parent do not ring, and the first poll only learns the newest id. Saved per browser, off by
  default; the tab must be open (no push server, nothing leaves the DC).
- **Phone (on-call)**: open `https://<appliance>/app/` on the phone and choose *Install app* / *Add
  to Home Screen*. It opens on **On-call**, full screen, with shortcuts to Incidents and NOC. The
  service worker (`web/public/sw.js`) only shows an offline page when the appliance cannot be
  reached; it never caches API data. Installing needs HTTPS (or `localhost`); paging stays SMS, mail
  and chat.
- **Push to this device** (Settings › Appearance) appears when an Admin turns on
  `notifications.push.enabled`; it subscribes this browser or installed app to web push for new
  CRITICAL incidents, even with the tab closed. See [settings.md](settings.md#web-push-to-phones-and-browsers).

## Teams and saved views

- **Team** on Incidents filters by the asset's *owner* (Inventory › owner); the list of teams is
  every owner in inventory. The CSV export uses the same filter.
- **Views**: *Save view* stores the current tab, severity, search, team and days under a name
  (Analyst+). *Only me* keeps it personal; an Engineer can share it with a team, and then everyone
  sees it as "team · name". The owner or an Engineer deletes it. API `GET/POST /api/v1/views`,
  `DELETE /api/v1/views/{id}`.

## Known errors

Automation › **Known errors** is the knowledge base of recurring problems: title, how it shows,
workaround, permanent fix, link, open/fixed. An entry matches an incident by **alertname** and/or a
text the incident **title** contains (both when both are set). Matching entries show on the
incident page, and ForgeAI gets them as evidence (`known_error`). Engineers add them there or with
*Save as known error* on an incident (prefilled with the title, alertname and confirmed cause).
API `/api/v1/known-errors`; `./forgesre known-errors [TEXT]`.

## Running an incident: major incidents, SLA, handover, follow-up actions

- **Major incident** (incident page, Analyst and up): *Declare major incident* with a commander,
  a bridge call link (`https://` or `tel:`) and an update interval. The card shows the next
  status update due (red when overdue); *Post status update* saves a note, restarts the timer
  and sends the update to the chat channels. *Message for management* is filled in from the
  incident, ready to copy. Major incidents show as a red strip on the NOC page and the wall
  screen. API: `POST /api/v1/incidents/{n}/major` `{on, commander, bridge_url, update_minutes}`,
  `POST /api/v1/incidents/{n}/updates` `{text}`.
- **SLA** (Settings › SLA, `sla.response.*` and `sla.resolve.*` minutes per severity; defaults
  critical 15/240, high 30/480, warning 120/1440): chips on the incident page (response due /
  met / breached, then resolve), a *breached* chip in incident lists, and the SLA card on
  Reports. SLA does not change escalation. `GET /api/v1/sla?days=30`, `./forgesre sla`.
- **Shift handover** (NOC page): *Write handover* saves notes and the incidents still open;
  the next person presses *I take over* (not the writer). `GET/POST /api/v1/handover`,
  `POST /api/v1/handover/{id}/take`, `./forgesre handover`.
- **Follow-up actions** (incident page, and *Open follow-up actions* on Reports): what must
  change, owner, due date, ticked off when done; they fill the postmortem draft's table.
  `POST /api/v1/incidents/{n}/actions`, `PATCH /api/v1/actions/{id}`, `GET /api/v1/actions`,
  `./forgesre actions`.

## TV mode (wall screen)

`/app/tv` (button **Wall screen (TV mode)** on the NOC page) is built for a screen on the wall: one
16:9 page with no tabs and no scrolling, sized from the screen so it reads the same on a monitor and
a 65" 4K TV, refreshed every 30 seconds.

- Top: the time (the appliance's time zone), open, not acknowledged, critical, hosts down, who is
  on call with the phone, and **Live** (red **No updates** when the page has not refreshed for 90 s).
- Left, two thirds: **Burning now**, eight rows: critical first, unacknowledged before
  acknowledged, oldest first; severity, title, host, number, tier, how long it has been open and
  who acknowledged it (red *not acked*). More than eight shows "+N more not shown". A critical
  that appears while the page is open glows for a minute. Nothing open: a calm **All quiet** with
  the time since the last incident was resolved.
- Right: the last 7 days as small "weather" cells (sun = quiet, cloud = warning, storm = high or
  critical; big number = incidents, small = criticals), the 12-hour incident clock, and the host
  groups (green when all answer, red with up/total when not).
- Bottom: **Last 24 hours** as a slim curve (incidents open per hour, red dots for hours with a
  critical, maintenance shaded, the share acknowledged within 15 minutes in the heading), then one
  row of small tiles: time to acknowledge, time to resolve, error budget, shift, last backup,
  noisiest assets, ForgeAI, hosts answering, new devices. Each shows one value and one coloured
  word; the sentence and detail are the tooltip.
- Colours follow the theme. A browser that never chose one shows the TV in **Navy**;
  `/app/tv?theme=contrast` (or `signal`, `light`, `midnight`) picks another for that screen only.
- Portrait or small windows (narrower than 4:3 or 900 px) get a scrolling one-column version.
- Quiet incidents (maintenance, noise filters) are not counted.

A Viewer account is enough: sign the wall computer in with one that cannot change anything.
Data: `GET /api/v1/tv` (the NOC overview plus `clock`, and `days`, `hours`, `tiles` from
`backend/app/tv_view.py`; database reads only, no other system is called).

## Developing

```bash
cd web
npm install
FORGESRE_DEV_CORE=http://127.0.0.1:8080 npm run dev   # proxies /api, /login, /logout to Core
npm run typecheck && npm test && npm run build
```

Sign in once on the Core address, then open the Vite address; the cookie is shared through
the proxy.

## Style

The "Signal" style approved on the design canvas: near-black console (`--bg #0a0b0d`) with a
faint dot grid, cards on `--s1`, monospace uppercase labels for sections, stats and table
headers, large tabular numbers, one cyan accent (`--accent #4fc3f7`). Severity colours also
differ in lightness, so critical, high and warning read apart without colour. Every token is
in `web/src/styles.css`; components use the tokens, never raw colours, and motion stops for
`prefers-reduced-motion`.

## Layout

- `src/components/Shell.tsx`: sidebar menu in the order of the designs; the version next to
  the name is `FORGESRE_VERSION` from `.env`, served by `GET /api/v1/me`. The menu scrolls on its
  own when the window is short; **☰** (top left) folds it away so the page uses the full width,
  remembered per browser.
- `src/components/ui.tsx`: Chip, Card, Stat, Bar, Dot.
- `src/styles.css`: design tokens (colours, radius, fonts) and base styles.
- `src/pages/*`: one file per page; unknown addresses show `NotFound`.
- `src/lib/api.ts`: fetch wrapper and the API types.

## Status

| Page | New UI |
|---|---|
| NOC overview | done |
| Incidents: list, detail, checklist, notes, Send report by mail | done |
| On-call | done: who is on call now, the week grid, add (also on several days at once), edit, copy, turn off, remove, take the rest of today |
| Inventory: library (site › rack, type, environment, customer, views), table, asset card with 30-day capacity (disk days to full, interface receive), add / edit / clone / delete, Import CSV (check first, then add in a background job; existing assets are skipped) | done |
| Automation: pipeline, noise filters (create, edit, remove), routing rules, escalation, playbooks, maintenance windows, "Try an alarm" | done, including creating, editing, turning off and removing rules and playbooks, escalation policies (Tier 1–3 and Manager) and maintenance windows (several assets at once, End now) |
| Investigations: resolved incidents with confirmed cause and ratings, accuracy, recurring causes; review card on the incident page | done |
| Reports: period KPIs vs previous, incidents per day, by asset type, noisiest assets, frequent alerts, host-down time, scheduled reports (create, edit, on/off, remove; presets, custom interval, cron, once), CSV | done |
| Settings: groups from the registry with inline editing, secret status, connection tests, services and health | done, including Users and roles (create, edit, role, password, remove; yourself and the last Admin are protected) and Backup (background backup, download, upload, remove, restore with the warning ticked and RESTORE typed) and Demo (lab scenarios as the `demo_run` job, Lower demo gauges) |

## API used by the new pages

| Page | Read | Write |
|---|---|---|
| NOC overview | `GET /api/v1/noc` | — |
| Incidents list | `GET /api/v1/incidents-page?tab=&severity=&q=&limit=&offset=` | — |
| Inventory | `GET /api/v1/inventory?q=&site=&rack=&type=&environment=&customer=&view=` · `GET /api/v1/inventory/{asset_id}` · `GET /api/v1/inventory-form` (types, extra fields, known environments, owners, sites) · `GET /api/v1/assets/import/template` · `GET /api/v1/assets/{id}/capacity[?refresh=true]` (Viewer+; read by the `asset_capacity` job, cached 15 min; `state` pending → poll, ready, error; also `./forgesre capacity ASSET`) | `POST /api/v1/assets`, `POST /api/v1/assets/{id}`, `POST …/clone`, `POST …/delete` (Engineer+). `POST /api/v1/inventory/bulk-delete` `{ids}` (Engineer+, each asset removed or refused with the reason), `GET /api/v1/inventory.csv` (same filters, Import CSV columns), `POST /api/v1/assets/import/preview` `{csv}` (writes nothing) and `POST /api/v1/assets/import` `{csv}` (checks again, queues `asset_import`; poll `GET /api/v1/onboard/jobs/{id}`), Engineer+. The form sends back saved extras it does not show, so support and maintenance settings survive an edit. |
| Automation | `GET /api/v1/automation`, `GET /api/v1/noise-filters` | `POST/PUT/DELETE /api/v1/noise-filters[/{id}]`, `POST/PUT/DELETE /api/v1/automation/rules[/{id}]` `{name, alertname, metric, operator, value, severity, playbook_id, escalation_policy_id, enabled}`, `POST/PUT/DELETE /api/v1/automation/playbooks[/{id}]` `{name, description, steps: [title]}` `POST/PUT/DELETE /api/v1/automation/policies[/{id}]` `{name, levels: [{level, on, minutes, target}]}` (removing a policy moves its rules to default-warning, which cannot be removed) `POST /api/v1/maintenance` `{assets, starts_at, ends_at}` and `DELETE /api/v1/maintenance/{asset_id}` (write_assets) (all Engineer+; removing a rule or playbook keeps incidents and clears the link), `POST /api/v1/automation/simulate` `{alertname, asset, value, at}` (read-only test) |
| Investigations | `GET /api/v1/investigations-page?days=&q=&only_ai=&only_reviewed=` · Ask ForgeAI: `GET /api/v1/incidents/{number}/forgeai`, `GET /api/v1/forgeai/skills` | `POST /api/v1/incidents/{number}/review` `{root_cause, rca_rating, ai_rating}` (correct / partly / wrong) · `POST /api/v1/incidents/{number}/forgeai/ask` `{skill?, question?}` (Analyst+, a trial) · `POST /api/v1/forgeai/calls/{id}/use` (Engineer+) |
| ForgeAI | `GET /api/v1/forgeai/chats` · `GET /api/v1/forgeai/chats/{id}` (your own chats only) | `POST /api/v1/forgeai/chats` `{title?, incident?, reuse?}` · `POST /api/v1/forgeai/chats/{id}/messages` `{text, skill?, cloud?}` (Analyst+; cloud Engineer+) · `DELETE /api/v1/forgeai/chats/{id}` · status line `GET /api/v1/forgeai/pulse`, test `POST /api/v1/forgeai/ping` |
| Reports | `GET /api/v1/reports-page?days=` · `GET /api/v1/reports/incidents.csv?days=` | `POST /api/v1/scheduled-reports` `{name, recipients, schedule, every_n, every_unit, weekdays, cron, asset_ids}`, `PUT …/{id}`, `POST …/{id}/toggle`, `DELETE …/{id}` (Analyst+) |
| Settings | `GET /api/v1/settings` (Admin) · `GET /api/v1/system/doctor` · `GET /api/v1/users-page` · `GET /api/v1/backups` | `PUT /api/v1/settings/{key}` `{value}` · `POST /api/v1/settings/test/{component}` · `POST /api/v1/users`, `POST /api/v1/users/{id}`, `POST …/{id}/delete` · `POST /api/v1/backups` (queues a `backup_create` job), `POST /api/v1/backups/import`, `DELETE /api/v1/backups/{name}` · `GET /api/v1/demo/scenarios`, `POST /api/v1/demo/run` `{scenario}`, `GET /api/v1/demo/jobs/{id}`, `POST /api/v1/demo-reset` (all Admin) |
| On-call | `GET /api/v1/oncall-week` | `POST /api/v1/oncall-slots` `{weekday, start, end, name, surname, email, phone, phone2}`, `PUT …/{id}`, `POST …/{id}/toggle`, `DELETE …/{id}`, `POST /api/v1/oncall-slots/take` (Engineer+) |
| Incident detail | `GET /api/v1/incidents/{number}/view` | `POST …/status` (acknowledge = `INVESTIGATING`, `ESCALATED`, `RESOLVED`), `POST …/notes`, `POST …/checklist` `{step, done}`, `POST …/investigate` (queues ForgeRCA, then ForgeAI), `GET …/mail` (subject, generated report, address suggestions, earlier sends) and `POST …/mail` `{to, subject, body}` (Analyst+, up to 10 addresses; queues `incident_mail`; poll `GET …/mail/{job}`) |

## Next

All pages exist and do their own editing; the classic UI was removed. Network discovery
comes from LibreNMS ([discovery.md](discovery.md)).

## Screenshots for the docs

`docs/images/ui-*.png` come from `scripts/screenshots.py` (Playwright, 1440 px wide). Run it
against a demo install with some incidents, then commit the new images:

```bash
pip install playwright && playwright install chromium
python scripts/screenshots.py --base http://127.0.0.1:8080 --email admin@forgesre.local --password '…'
python scripts/screenshots.py --only noc,incident     # just a few pages
```

## Escalation names

The new UI names escalation steps the way most NOCs do: **Tier 1** (NOC), **Tier 2** (shift
lead), **Tier 3** (engineer), then **Manager**, which is not a support tier. The database and
the API keep levels 1–4 (L1–L4); `tierLabel()` in `web/src/lib/format.ts` maps them.

## Add devices

`/app/inventory/add` (Engineer). Three tabs: one device, from LibreNMS (ForgeSRE never scans networks; see [discovery.md](discovery.md)), from another system.

- `POST /api/v1/onboard/identify` `{ip, snmp_port, snmp: {snmp_version, snmp_community_mode,
  snmp_community | snmp_v3_*}}` queues an `onboard_identify` job: TCP and exporter probes, and a
  v1/v2c SNMP GET of sysDescr, sysObjectID, sysName, sysLocation (`app/snmp_get.py`, stdlib).
  The SNMP secrets are dropped from the job when it finishes or fails.
- `POST /api/v1/assets/{id}/verify-job` queues `asset_verify` (the live chain as a checklist,
  `app/onboarding.py`) and, when the asset's SNMP auth is not in snmp.yml yet, `snmp_apply`.
- `GET /api/v1/onboard/jobs/{id}`: status and result of those jobs.
- `GET /api/v1/discovery/status`: LibreNMS ready (or why not), mode, the running and last read,
  found counts. `POST /api/v1/discovery/scan` queues a LibreNMS read (`discovery_scan` job);
  `POST /api/v1/discovery/candidates/{id}/approve|ignore` decide a found device.
- `snmp_apply` (`app/snmp_apply.py`) writes per-asset SNMP auths into the generated snmp.yml
  that Core mounts at `/host-generated` and POSTs `/-/reload` to snmp_exporter. Saving an asset
  queues it, and the leader queues it every 5 minutes while an auth is pending.

## Incident page, more

`GET /api/v1/incidents/{number}/view` also returns `who_to_call` (on call now, asset contact,
backup contact, vendor support), `similar` (earlier incidents with the same title on the same
asset, with the confirmed cause and minutes to resolve) and `events` (alarm, timeline, mails and
chat, notes, playbook ticks, ack and resolve, in time order). Host metrics come from `GET
/api/v1/assets/{id}/metrics?incident=…`; syslog from `GET /api/v1/incidents/{number}/syslog`
(Analyst and up, Loki, 30 minutes either side of the start, loaded only when asked).

## Lists and activity

- `GET /api/v1/incidents-page` takes `date_from` / `date_to` (YYYY-MM-DD, appliance time zone);
  `GET /api/v1/incidents-page.csv` returns the same filtered list as CSV with a UTF-8 BOM.
- `GET /api/v1/outbox?status=failed|sent&q=` (Analyst and up), `GET /api/v1/audit?q=` (Admin),
  `GET /api/v1/journal` (Analyst and up) back the outbox on Reports and Settings › Journal and
  Audit log.
