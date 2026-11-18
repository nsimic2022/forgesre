# ForgeSRE 2 — roadmap and backlog

Plan agreed in November 2026. Page designs: the "ForgeSRE 2 — izgled stranica" canvas.
Each step ships as its own pull request, merged when CI is green (AGENTS.md).

## Phase 1 — backend foundation

1. ~~Four roles: Viewer, Analyst, Engineer, Admin~~ (done, #148).
2. ~~Background jobs and timeouts~~ (done: one leader process, atomic claim, LLM lane, DB sign-in throttle). Remaining: a Postgres-backed job queue and a separate worker
   process for ForgeAI, discovery, syncs, reports and backups; a timeout on every
   external call. Move the sign-in throttle and session state to the database so Core
   can run several web workers.
3. Settings module: network, services, integrations, notification channels, AI, stored
   in the database, written by the installer, editable in the UI, readable with
   `./forgesre settings`. Secrets write-only.

## Phase 2 — new UI, page by page

NOC overview → Incidents and on-call → Inventory → Automation → Investigations →
Reports → Settings. The current UI stays at `/classic` until each page is replaced.

## Phase 3 — automation and reports

Noise filters (windows, short spikes, known issues, upstream dependency), routing,
escalation policies, the "try an alarm" simulator, scheduled reports (daily, weekly,
monthly, cron) by email, Slack, Teams, Discord.

## Phase 4 — enterprise

LDAP / AD and OIDC sign-in, air-gapped install bundle, high availability.

## Backlog

### Asset sources: one inventory, many feeders

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

### Discovery must never print on printers (known bug)

Cause: discovery probes TCP 9100 and sends `GET /metrics` to `http://<ip>:9100`
(`exporter_detect.py`, `asset_probe.py`) to recognise node_exporter. On a network
printer, TCP 9100 is the raw print port (JetDirect): the printer prints whatever
arrives, so every probe prints the HTTP request as a page, in large type, again on
each scan. A Prometheus scrape job pointed at a printer IP would do the same every
scrape interval.

Fix:

- Network discovery moves to LibreNMS (SNMP only). ForgeSRE stops sweeping subnets
  with TCP connects.
- Before any HTTP to :9100, ask SNMP first; skip devices whose sysObjectID or
  hrDeviceType is a printer, and devices already in inventory as printers.
- Never put a printer into a Prometheus node_exporter job; printers are monitored by
  SNMP (Printer-MIB: toner, paper, errors).
- A test that a printer-like target never receives a TCP connection on 9100.

### Other items

- Docstrings module by module as code is reorganised (AGENTS.md §1).
- Rewrite and shorten the English docs page by page with each UI change.
- TV mode: full-screen NOC view for a wall screen on a Viewer account.
- OpenRCA as a test bench for ForgeRCA / ForgeAI accuracy, not a knowledge base.
- Analyst rating of each analysis (correct / partly / wrong) to measure accuracy.
