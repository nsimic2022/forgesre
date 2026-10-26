# ForgeSRE architecture (V0.9 appliance)

How the appliance **runs today** on one Ubuntu VM: Docker Compose, host networking, one Core process.

Operators: [operator-handbook.md](operator-handbook.md) and [cli.md](cli.md). Install and update: [install-config.md](install-config.md). What this version added: [v0.9.md](v0.9.md).

---

## 1. What is running

Docker Compose, **host network**, one VM. Core is Python (FastAPI + Jinja2). The CLI is Bash (`./forgesre`). Postgres holds ForgeSRE data (users, assets, incidents, rules, jobs, audit). Redis is the bundled NetBox cache.

| Piece | Role |
|---|---|
| **Core** | UI, API, ingest, mail, one background worker. Listens on the management port (default `:8080`). |
| **Postgres** | Users, assets, incidents, rules, jobs, audit. `127.0.0.1:5432`. |
| **Prometheus** | Scrapes. `127.0.0.1:9090`. |
| **Alertmanager** | Groups alerts and webhooks Core. `127.0.0.1:9093`. |
| **snmp_exporter** | Walks network devices. `127.0.0.1:9116`. |
| **Grafana Alloy** | Receives device syslog. Own UI `127.0.0.1:12345`. Syslog **UDP/TCP 514**. |
| **Loki** | Stores those logs. `127.0.0.1:3100`. |
| **Grafana** | Extra graphs for an engineer (`:3000`), opened from System Health. |
| **NetBox** | Bundled DCIM (`:8001`) plus its Redis and Postgres. Read-only from Core. |
| **llama.cpp** | Optional profile `ai`. Prose rewrite. |

Loopback UIs (Prometheus, Alertmanager, snmp_exporter, Loki, Alloy, the LLM) stay on `127.0.0.1`. Use them on the VM or over SSH.

---

## 2. One Core process

Compose starts **one** `core` service: one uvicorn process.

Inside that process:

- The HTTP server serves the UI and the API.
- One **jobs thread** claims rows from the Postgres `jobs` table (RCA, discovery scan, scheduled reports). Claim is pending-then-running.
- The same process runs the escalation loop and the discovery loop as threads.

Keep a **single** Core replica. A second process would run a second escalation loop and send the same mail twice. The same applies to extra uvicorn workers: each worker would run those loops. Throughput is bounded by scrape and syslog volume (see [Scale](#7-scale)).

An LLM rewrite can occupy the jobs thread until `ai.llm.timeout_seconds`. That is why reports are claimed before an LLM job.

---

## 3. Alarm path

```text
exporter (node_exporter :9100, windows_exporter :9182, or snmp_exporter)
        → Prometheus
        → Alertmanager
        → Core webhook
        → incident, Rules, Escalation mail
```

Host metrics on the Dashboard and the incident page are drawn by Core from Prometheus `query_range` (or Zabbix `trend.get` when Prometheus has no samples). Grafana (`:3000`) is an extra graph UI; Alertmanager still webhooks Core if Grafana is down.

The Dashboard **ForgeAI** strip aggregates incident rows in SQL, then optionally asks the local LLM for a few sentences. Ingest, mute, and mail stay on the path above.

**Second path (optional):** Zabbix webhook → Core. Same incident lifecycle. Core reads Zabbix (`host.get`, `problem.get`, `item.get`, `trend.get`). Recovery resolves the incident; a backup `problem.get` poll covers a lost recovery webhook.

**Syslog path.** Alloy listens on 514, labels syslog by hostname and sender IP, and pushes to Loki. Core demo logs stay `job="forgesre"` on `forge-demo-01`. Loki lines appear as incident evidence. Alarms still come from exporters → Prometheus → Alertmanager. Node exporter and snmp_exporter remain metric sources; Alloy receives syslog.

---

## 4. Graphs

The chart uses the scrape identity Prometheus actually stored:

- Linux / Windows HTTP SD: `job` is the monitoring profile (`linux-standard`, `windows-standard`) and `instance` is `scrape_address` (`ip:9100` or `ip:9182`).
- SNMP HTTP SD: `job="forgesre-snmp"` and `instance` is the device address (bare IP on UDP/161, `ip:port` otherwise).

`asset="<id>"` is the next matcher. An empty scrape address stays out of exporter SD until Verify or Edit sets one.

| What you see | Meaning |
|---|---|
| A line that moves | Prometheus (or Zabbix trends) returned samples in the chart window. |
| Vertical line | Incident start (When), when that time is known. |
| **Steady** | Samples exist and the value held still. |
| **Not scraped.** | This host is outside Prometheus HTTP SD. |
| **No samples yet.** | The query ran and that window is empty. |

The asset page charts the last hour. Dashboard and the incident page start one hour before the incident and run through now, or through the end if it is resolved. A series that ran and then dropped to 0, or pegged, stays in that window.

Alarms **mute** skips ingest of a bundled alert. The chart still shows the series.

---

## 5. Rules, playbooks, alarms, escalation

| Name | What it does |
|---|---|
| **Rules** (`/playrules`) | After ingest, match **alertname** and attach a Playbook plus the default escalation. PromQL stays in `monitoring/alerts.yml`. |
| **Playbooks** | A checklist a person reads on the incident. |
| **Alarms** | Per-asset mute (and a percent note) for bundled alerts. Off means Core skips that incident. |
| **Custom alarms** | Per-asset list. First ON rule with the same alertname wins over Rules. OFF skips that rule on this host only. |
| **Escalation** | Per-asset mail steps L1–L4 from incident start until Acknowledge. Empty uses the Default escalation. Email only. |

`monitoring/alerts.yml` is the PromQL. Thresholds are evaluated there.

**Incidents** is the incident list. `/history` redirects here. Status `ESCALATED` means the mail steps have climbed. The Dashboard cube counts those rows. Acknowledge keeps `ESCALATED`.

---

## 6. Read-only edges

- **NetBox** — Core reads devices (`GET /api/dcim/devices/`). API 403 with the UI up is a warn on System Health.
- **Zabbix** — read-only token, webhook in. Ack / Resolve / Close stay in ForgeSRE.
- **LLM** — optional. It rewrites investigation prose from compact facts Core already computed.

---

## 7. Scale

One VM, one Core.

Prometheus, Alloy, and Loki absorb scrape and log volume. The practical ceiling around a thousand hosts is:

- **Syslog on 514** — how much log traffic Alloy and Loki can take.
- **Prometheus cardinality** — one series set per scraped target (node, windows, SNMP `if_mib`).

---

## 8. Failure, in one pass

| If this stops | What still works | What you lose |
|---|---|---|
| Core | Prometheus still scrapes; Alertmanager still fires | New incidents, UI, mail |
| Postgres | Metrics and logs | Core |
| Prometheus or Alertmanager | Inventory, existing incidents, logs | New metric alerts |
| Grafana | The alarm path and Core's own charts | The Grafana UI |
| Loki or Alloy | Alarms and metrics | New syslog lines |
| NetBox or Zabbix | Local inventory and the Prometheus path | That integration |
| LLM | Incidents and ForgeRCA facts | The prose rewrite |
