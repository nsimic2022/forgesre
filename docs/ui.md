# New UI (ForgeSRE 2)

React + TypeScript in `web/`, built by Vite into `web/dist` and served by Core at `/app`.
It is the home page: `/` sends a signed-in user to `/app/` when the build is present and
`ui.home` is `new` (the default). The classic dashboard is always at `/classic`, and the
other classic pages keep their paths. `./forgesre settings set ui.home classic` switches
the home page back.

## How it runs on the appliance

- `docker compose build core` (and `./forgesre update` when `web/` changes) builds the UI in
  a Node stage of `backend/Dockerfile`; the Core image only carries the built files.
- The browser talks to Core alone. Fonts are bundled (`@fontsource`); no CDN, no analytics,
  nothing leaves the appliance.
- Data comes from `/api/v1/*` with the normal session cookie. A 401 sends the browser to
  `/login`. Each page reads one compact endpoint (NOC: `GET /api/v1/noc`, polled every 10 s).
- The classic sidebar shows **New UI** when the build is present; the new UI links to **Open classic UI** (`/classic`).

## Developing

```bash
cd web
npm install
FORGESRE_DEV_CORE=http://127.0.0.1:8080 npm run dev   # proxies /api, /login, /logout to Core
npm run typecheck && npm test && npm run build
```

Sign in once on the Core address, then open the Vite address; the cookie is shared through
the proxy.

## Layout

- `src/components/Shell.tsx`: sidebar menu in the order of the designs.
- `src/components/ui.tsx`: Chip, Card, Stat, Bar, Dot.
- `src/styles.css`: design tokens (colours, radius, fonts) and base styles.
- `src/pages/*`: one file per page; pages not rebuilt yet link to their classic page.
- `src/lib/api.ts`: fetch wrapper and the API types.

## Status

| Page | New UI |
|---|---|
| NOC overview | done |
| Incidents: list, detail, checklist, notes | done |
| On-call | done: who is on call now, the week grid, add (also on several days at once), edit, copy, turn off, remove, take the rest of today |
| Inventory: library (site › rack, type, environment, customer, views), table, asset card, add / edit / clone / delete | done; SNMP version and credentials, discovery and imports still on the classic pages |
| Automation: pipeline, noise filters (create, edit, remove), routing rules, escalation, playbooks, maintenance windows, "Try an alarm" | done, including creating, editing, turning off and removing rules and playbooks, escalation policies (L1–L4) and maintenance windows (several assets at once, End now) |
| Investigations: resolved incidents with confirmed cause and ratings, accuracy, recurring causes; review card on the incident page | done |
| Reports: period KPIs vs previous, incidents per day, by asset type, noisiest assets, frequent alerts, host-down time, scheduled reports (create, edit, on/off, remove; presets, custom interval, cron, once), CSV | done |
| Settings: groups from the registry with inline editing, secret status, connection tests, services and health | done, including Users and roles (create, edit, role, password, remove; yourself and the last Admin are protected) and Backup (background backup, download, upload, remove; restore stays a confirmed step on the classic Admin page) |

## API used by the new pages

| Page | Read | Write |
|---|---|---|
| NOC overview | `GET /api/v1/noc` | — |
| Incidents list | `GET /api/v1/incidents-page?tab=&severity=&q=&limit=&offset=` | — |
| Inventory | `GET /api/v1/inventory?q=&site=&rack=&type=&environment=&customer=&view=` · `GET /api/v1/inventory/{asset_id}` · `GET /api/v1/inventory-form` (types, extra fields, known environments, owners, sites) | `POST /api/v1/assets`, `POST /api/v1/assets/{id}`, `POST …/clone`, `POST …/delete` (Engineer+). The form sends back saved extras it does not show, so support and maintenance settings survive an edit. |
| Automation | `GET /api/v1/automation`, `GET /api/v1/noise-filters` | `POST/PUT/DELETE /api/v1/noise-filters[/{id}]`, `POST/PUT/DELETE /api/v1/automation/rules[/{id}]` `{name, alertname, metric, operator, value, severity, playbook_id, escalation_policy_id, enabled}`, `POST/PUT/DELETE /api/v1/automation/playbooks[/{id}]` `{name, description, steps: [title]}` `POST/PUT/DELETE /api/v1/automation/policies[/{id}]` `{name, levels: [{level, on, minutes, target}]}` (removing a policy moves its rules to default-warning, which cannot be removed) `POST /api/v1/maintenance` `{assets, starts_at, ends_at}` and `DELETE /api/v1/maintenance/{asset_id}` (write_assets) (all Engineer+; removing a rule or playbook keeps incidents and clears the link), `POST /api/v1/automation/simulate` `{alertname, asset, value, at}` (read-only test) |
| Investigations | `GET /api/v1/investigations-page?days=&q=&only_ai=&only_reviewed=` | `POST /api/v1/incidents/{number}/review` `{root_cause, rca_rating, ai_rating}` (correct / partly / wrong) |
| Reports | `GET /api/v1/reports-page?days=` · `GET /api/v1/reports/incidents.csv?days=` | `POST /api/v1/scheduled-reports` `{name, recipients, schedule, every_n, every_unit, weekdays, cron, asset_ids}`, `PUT …/{id}`, `POST …/{id}/toggle`, `DELETE …/{id}` (Analyst+) |
| Settings | `GET /api/v1/settings` (Admin) · `GET /api/v1/system/doctor` · `GET /api/v1/users-page` · `GET /api/v1/backups` | `PUT /api/v1/settings/{key}` `{value}` · `POST /api/v1/settings/test/{component}` · `POST /api/v1/users`, `POST /api/v1/users/{id}`, `POST …/{id}/delete` · `POST /api/v1/backups` (queues a `backup_create` job), `POST /api/v1/backups/import`, `DELETE /api/v1/backups/{name}` (all Admin) |
| On-call | `GET /api/v1/oncall-week` | `POST /api/v1/oncall-slots` `{weekday, start, end, name, surname, email, phone, phone2}`, `PUT …/{id}`, `POST …/{id}/toggle`, `DELETE …/{id}`, `POST /api/v1/oncall-slots/take` (Engineer+) |
| Incident detail | `GET /api/v1/incidents/{number}/view` | `POST …/status` (acknowledge = `INVESTIGATING`, `ESCALATED`, `RESOLVED`), `POST …/notes`, `POST …/checklist` `{step, done}`, `POST …/investigate` (queues ForgeRCA, then ForgeAI) |

## Next

All seven pages exist and do their own editing. Still on classic pages: discovery, asset
SNMP versions and credentials, and restoring a backup. The new UI becomes the home page next.
