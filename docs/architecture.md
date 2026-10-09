# ForgeSRE architecture (V0.9 appliance)

This is how the appliance **runs today** on one Ubuntu VM. It is not a proposal and not a backlog.

Operators: [operator-handbook.md](operator-handbook.md) and [cli.md](cli.md). Install and update: [install-config.md](install-config.md). What this version added: [v0.9.md](v0.9.md).

**Not shipped:** Celery, a second Core container, extra uvicorn workers, a Go service, Kubernetes, and Caddy as the product entrypoint. Do not build those from old notes.

---

## 1. What is running

Docker Compose, **host network**, one VM. Core is Python (FastAPI + Jinja2). The CLI is Bash (`./forgesre`). Postgres is the only ForgeSRE database. Redis exists only because bundled NetBox needs it. It is not a job broker.

| Piece | Role |
|---|---|
| **Core** | UI, API, ingest, mail, one background worker. Listens on the management port (default `:8080`). |
| **Postgres** | Users, assets, incidents, rules, jobs, audit. `127.0.0.1:5432`. |
| **Prometheus** | Scrapes. `127.0.0.1:9090`. |
| **Alertmanager** | Groups alerts and webhooks Core. `127.0.0.1:9093`. |
| **snmp_exporter** | Walks network devices. `127.0.0.1:9116`. |
| **Grafana Alloy** | Receives device syslog. Own UI `127.0.0.1:12345`. Syslog **UDP/TCP 514**. |
| **Loki** | Stores those logs. `127.0.0.1:3100`. |
| **Grafana** | Optional graphs for an engineer. Not in the alarm path. |
| **NetBox** | Bundled DCIM (`:8001`) plus its Redis and Postgres. Read-only from Core. |
| **llama.cpp** | Optional profile `ai`. Prose only. |

Loopback UIs (Prometheus, Alertmanager, snmp_exporter, Loki, Alloy, the LLM) stay on `127.0.0.1`. Do not publish them on the management network. Use them on the VM or over SSH.

---

## 2. One Core process

Compose starts **one** `core` service. The command is one uvicorn process, not `--workers`, and not a second replica.

Inside that process:

- The HTTP server serves the UI and the API.
- One **jobs thread** claims rows from the Postgres `jobs` table (RCA, discovery scan, scheduled reports). Claim is pending-then-running. There is no Celery and no Redis queue for ForgeSRE.
- The same process runs the escalation loop and the discovery loop. They are threads, not extra containers.

**Do not clone Core.** A second replica would run a second escalation loop and **send the same mail twice**. Do not add uvicorn workers for the same reason: each worker would run those loops. Throughput is not the limit (see [Scale](#7-scale)).

An LLM rewrite can occupy the jobs thread until `ai.llm.timeout_seconds`. That is why reports are claimed before an LLM job. It is still one thread, not a worker pool.

---

## 3. Alarm path

```text
exporter (node_exporter :9100, windows_exporter :9182, or snmp_exporter)
        → Prometheus
        → Alertmanager
        → Core webhook
        → incident, Rules, Ladder mail
```

Grafana is **not** on this path. A Grafana outage does not stop alerts. Host metrics on the Dashboard and the incident page are drawn by Core from Prometheus `query_range` (or Zabbix `trend.get` when Prometheus has no samples). There is no Grafana iframe.

**Second path (optional):** Zabbix webhook → Core. Same incident lifecycle. ForgeSRE does not write back to Zabbix. Recovery resolves the incident; a backup `problem.get` poll covers a lost recovery webhook.

**Logs are not the alarm path.** Alloy listens on 514, labels syslog by hostname and sender IP, and pushes to Loki. Core demo logs stay `job="forgesre"` on `forge-demo-01`. Loki lines can show up as incident evidence. They do not open an incident. Node exporter and snmp_exporter are not log sources. Alloy does not scrape them.

---

## 4. Graphs

The chart uses the scrape identity Prometheus actually stored:

- Linux / Windows HTTP SD: `job` is the monitoring profile (`linux-standard`, `windows-standard`) and `instance` is `scrape_address` (`ip:9100` or `ip:9182`).
- SNMP HTTP SD: `job="forgesre-snmp"` and `instance` is the device address (bare IP on UDP/161, `ip:port` otherwise).

`asset="<id>"` is the next matcher. An empty scrape address is not in exporter SD, and the chart does not invent `ip:9100`.

| What you see | Meaning |
|---|---|
| A line that moves | Prometheus (or Zabbix trends) returned that hour. |
| **Steady** | Samples exist and did not move. Not a placeholder. |
| **Not scraped.** | This host is not in Prometheus. |
| **No samples yet.** | The query is honest and the hour is empty. |

Standard alarms **mute** only skips ingest of a bundled alert. Mute does not flatten or hide the chart. Core does not draw a sine wave when the hour is empty.

---

## 5. Rules, playbooks, alarms, ladder

| Name | What it does | What it does not do |
|---|---|---|
| **Rules** (`/playrules`) | After ingest, match **alertname** and attach a Playbook plus the default ladder. | Create Prometheus rules. Run the playbook. |
| **Playbooks** | A checklist a person reads. | SSH, scripts, or auto-remediation. |
| **Standard alarms** | Per-asset mute (and a percent note) for bundled alerts. Off means Core does not open that incident. | Fire earlier than `alerts.yml`. Change the graph. |
| **Custom alarms** | Per-asset list. First ON rule with the same alertname wins over Rules. OFF skips that rule on this host only. | A second alerting engine. |
| **Ladder** | Per-asset mail steps L1–L4 from incident start until Acknowledge. Empty uses the Default ladder. Email only. | Webhooks, SMS, or chat. A second mail policy on top of Rules. |

`monitoring/alerts.yml` is the PromQL. Thresholds are not evaluated from the Rules form.

**Incidents** is the only incident list. History was merged there; `/history` redirects. Status `ESCALATED` means the ladder has climbed. The Dashboard cube counts those rows. Acknowledge does not clear `ESCALATED`.

---

## 6. Read-only edges

- **NetBox** — Core reads devices. It does not write inventory back. API 403 with the UI up is a warn, not a pause.
- **Zabbix** — read-only token, webhook in, no write-back on ack or resolve.
- **LLM** — optional. It rewrites investigation prose. It does not SSH, does not change devices, and does not send mail by itself.

---

## 7. Scale

Do **not** scale Core replicas. One VM, one Core.

Prometheus, Alloy, and Loki are what absorb scrape and log volume. The practical ceiling around a thousand hosts is:

- **Syslog on 514** — how much log traffic Alloy and Loki can take.
- **Prometheus cardinality** — one series set per scraped target (node, windows, SNMP `if_mib`), not the number of uvicorn workers.

Adding Core workers does not raise that ceiling, and it duplicates mail.

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
