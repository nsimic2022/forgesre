# ForgeSRE 2 — roadmap and backlog

Plan agreed in November 2026. Page designs: the "ForgeSRE 2 — izgled stranica" canvas.
Each step ships as its own pull request, merged when CI is green (AGENTS.md).

## Phase 1 — backend foundation

1. ~~Four roles: Viewer, Analyst, Engineer, Admin~~ (done, #148).
2. ~~Background jobs and timeouts~~ (done: Postgres job queue with atomic claim, leader lock,
   LLM lane, sign-in throttle and loop heartbeats in the database, several web workers
   (`FORGESRE_WEB_WORKERS`) and an optional separate worker process (`worker` profile)).
3. ~~Settings module, backend and CLI~~ (done: [settings.md](settings.md)). Live values in
   `config/forgesre.yml`, secrets via `./forgesre settings set-secret`, connection tests.
   The Settings page itself comes with the new UI. Later: notification channels for Slack,
   Teams and Discord, and secrets editable from the UI through a host-side helper.

## Phase 2 — new UI, page by page

React + TypeScript in `web/`, served at `/app` ([ui.md](ui.md)). Order: ~~NOC overview~~
(done) → ~~Incidents~~ (done; on-call week grid with Phase 3) → ~~Inventory~~ (done; editing next) → ~~Automation~~ (done; noise filters in Phase 3) → ~~Investigations~~ (done, with analyst reviews) → ~~Reports~~ (done; cron schedules in Phase 3) →
~~Settings~~ (done). The classic UI stays until each page is replaced, then moves to `/classic`.

## Phase 3 — automation and reports

Done ([alerting.md](alerting.md)): severities by urgency, predictive disk alerts, one incident
per problem, noise filters, routing rules, escalation policies (Tier 1–3, Manager), the "try an
alarm" simulator, scheduled reports (presets, interval, cron, once) by email, Slack, Teams and
Discord, the ForgeAI morning digest, 30-day capacity on the asset card.

## Phase 4 — enterprise

~~LDAP / AD and OIDC sign-in~~ (done, [sign-in.md](sign-in.md)), ~~air-gapped install bundle~~ (done, [offline.md](offline.md)), high availability (documented active / standby on a shared Postgres, [high-availability.md](high-availability.md); not an automated cluster).

## ForgeAI 2 — how investigation should work

Status: the investigator with read-only tools and evidence ids, the local knowledge search
(lexical, own confirmed incidents and playbooks), skill tuning and replay against confirmed causes
are done ([llm.md](llm.md)). Still open: pgvector semantic search with a local embedding model,
uploaded vendor documents, the cloud-only critic pass, and the same tools on the MCP server.

Agreed direction, after the new Investigations page exists (it shows ratings and accuracy).

- **ForgeRCA stays the fact collector, in Python.** Specialists run in parallel with a
  timeout each: metrics, logs, inventory, history (today), plus network/topology (SNMP and
  LibreNMS neighbours, uplinks), changes (maintenance windows, GLPI changes) and
  trend/capacity (time to full, error growth). No LLM in collection.
- **One ForgeAI investigator, not a chat of many LLM agents.** Local models take 20–60 s
  per call; a chain of agents would take minutes and invent more. The investigator gets
  read-only tools (Prometheus range query, Loki search, interface status, asset card,
  similar incidents), at most about six steps and a time budget, and must answer in a fixed
  structure: cause, confidence, evidence ids for every claim, next steps. Code checks that
  every cited evidence id exists; an answer that cites missing evidence is rejected or
  flagged. A second "critic" pass only in cloud mode. No automatic remediation.
- **Knowledge base, all local, in Postgres with pgvector** and a local embedding model:
  1. own incidents with the analyst-confirmed cause (most valuable, grows daily);
  2. playbooks and runbooks;
  3. uploaded vendor documents (manuals, syslog message catalogues);
  4. topology from NetBox / LibreNMS for blast radius (no Neo4j).
  Similar incidents: exact match first (alert, device type, vendor, syslog message id),
  then semantic search; the model gets the 3–5 closest with their confirmed fixes.
- **Learning loop.** The analyst confirms the cause and rates ForgeRCA and ForgeAI
  (correct / partly / wrong); stored on the incident since the Investigations page. Ratings feed similar-incident search and the
  accuracy figures on the Investigations page.
- **Measuring.** Replay the last 100 resolved incidents and compare with the confirmed
  cause. Microsoft OpenRCA (telemetry from lab micro-service systems with labelled root
  causes, no fixes, nothing about switches, UPS, RAID or ESXi) is a test bench only:
  installed apart under `/opt/forgesre/datasets/openrca` and run by `./forgesre rca-bench`,
  never searched at incident time.
- **Hardware.** A 24 GB GPU runs a mid-size local model well enough for the investigator.
  Without a GPU: ForgeRCA plus a small model for the summary, and the tool-using
  investigator only in cloud mode (masked, Critical/High, on request).

### ForgeSRE MCP server (read-only)

The tools the ForgeAI 2 investigator uses (incident, asset card, Prometheus range query,
Loki search, interface status, similar incidents) are also served as a Model Context
Protocol server, so an assistant the operator chooses can query the NOC with the same
rules: sign-in with a ForgeSRE account and its role, read-only, local network only, every
call audited. Built once for both.

## Backlog

### ~~Asset sources: one inventory, many feeders~~ (done, `app/asset_sources.py`)

Still open: moving network discovery to LibreNMS only (SNMP), instead of ForgeSRE's own scan.


Assets can come from manual entry, GLPI, Zabbix, LibreNMS, Prometheus targets and
Grafana / Alloy. Target design:

- One asset record per device, with a source per field: GLPI owns identity, location
  and contract; LibreNMS owns network discovery (sysName, sysObjectID, interfaces);
  Zabbix and Prometheus attach monitoring and alarms; manual edits win where the
  owner says so.
- Matching on serial, then MAC, then sysName/hostname, then IP. A mismatch (for example
  a different IP in GLPI and LibreNMS) becomes a conflict to review, never a silent
  overwrite or a duplicate.
- New devices land in Discovery as "found", never straight in inventory; an Engineer
  approves them.
- Each source shows last sync, counts and errors on its Settings card and in
  `./forgesre status`.

### ~~Discovery must never print on printers~~ (fixed)

TCP 9100 is a printer's raw print port. Every connect or HTTP request to 9100 now goes through
`app/printer_guard.py`: inventory printer or SNMP printer answer → never; no SNMP answer → only
with SSH open; printers never enter the node_exporter job. Tested in `tests/test_printer_guard.py`.
Moving network discovery to LibreNMS (SNMP only) stays open under Asset sources.

### Other items

- Docstrings module by module as code is reorganised (AGENTS.md §1).
- Rewrite and shorten the English docs page by page with each UI change.
- ~~TV mode~~ (done, with the incident clock).
- OpenRCA as a test bench for ForgeRCA / ForgeAI accuracy, not a knowledge base.
- ~~Analyst rating of each analysis~~ (done with the Investigations page).
- SLO per business service (for example ERP 99.9%): SLOs and error budgets exist
  (`./forgesre slo`); the per-service view needs services recorded on assets.
