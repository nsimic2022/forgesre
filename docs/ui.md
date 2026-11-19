# New UI (ForgeSRE 2)

React + TypeScript in `web/`, built by Vite into `web/dist` and served by Core at `/app`.
The classic UI keeps working at its current paths until each page is replaced; then the
new UI moves to `/` and the classic one to `/classic`.

## How it runs on the appliance

- `docker compose build core` (and `./forgesre update` when `web/` changes) builds the UI in
  a Node stage of `backend/Dockerfile`; the Core image only carries the built files.
- The browser talks to Core alone. Fonts are bundled (`@fontsource`); no CDN, no analytics,
  nothing leaves the appliance.
- Data comes from `/api/v1/*` with the normal session cookie. A 401 sends the browser to
  `/login`. Each page reads one compact endpoint (NOC: `GET /api/v1/noc`, polled every 10 s).
- The classic sidebar shows **New UI (preview)** when the build is present.

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
| On-call | who is on call now; the week grid comes with the escalation rework |
| Inventory: library (site › rack, type, environment, customer, views), table, asset card | done; editing still on the classic page |
| Automation: pipeline, routing rules, escalation, playbooks, maintenance windows, "Try an alarm" | done; editing still on the classic pages |
| Investigations: resolved incidents with confirmed cause and ratings, accuracy, recurring causes; review card on the incident page | done |
| Reports | next |
| Settings | planned |

## API used by the new pages

| Page | Read | Write |
|---|---|---|
| NOC overview | `GET /api/v1/noc` | — |
| Incidents list | `GET /api/v1/incidents-page?tab=&severity=&q=&limit=&offset=` | — |
| Inventory | `GET /api/v1/inventory?q=&site=&rack=&type=&environment=&customer=&view=` · `GET /api/v1/inventory/{asset_id}` | classic asset page for now |
| Automation | `GET /api/v1/automation` | `POST /api/v1/automation/simulate` `{alertname, asset, value, at}` (read-only test) |
| Investigations | `GET /api/v1/investigations-page?days=&q=&only_ai=&only_reviewed=` | `POST /api/v1/incidents/{number}/review` `{root_cause, rca_rating, ai_rating}` (correct / partly / wrong) |
| Incident detail | `GET /api/v1/incidents/{number}/view` | `POST …/status` (acknowledge = `INVESTIGATING`, `ESCALATED`, `RESOLVED`), `POST …/notes`, `POST …/checklist` `{step, done}`, `POST …/investigate` (queues ForgeRCA, then ForgeAI) |
