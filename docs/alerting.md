# Alerting principles

How ForgeSRE decides what wakes someone up. Applies to `monitoring/alerts.yml`, playrules,
noise filters and escalation. Read before adding or changing an alert.

## Rules

1. **Every alert needs an action.** If nobody would do anything, it is a dashboard or a
   report line, not an alert. Write the action in the rule's playbook.
2. **Severity is urgency, not size.**
   - `critical`: service or host down, or data at risk; someone acts now, day or night.
   - `high`: will become critical within hours if nobody acts (disk full in hours,
     redundancy lost).
   - `warning`: needs attention in working hours; never pages at night.
3. **Alert on symptoms first, causes second.** "Host unreachable" and "disk full in 4 h"
   before "CPU 91%". Cause alerts are evidence for ForgeRCA, not pages.
4. **Wait before firing.** Every rule has `for:` long enough to skip spikes (2–5 min; 10 min
   for CPU). Short spikes that still slip through are handled by the ForgeSRE noise filter.
5. **Predict rather than threshold** where the trend matters: disk and memory use
   `predict_linear` ("full within 6 h") next to a high static threshold.
6. **One problem, one incident.** Hosts behind a down switch, and every alert on a host
   that is down, attach to that one incident instead of opening their own.
7. **Known noise is filtered, not deleted.** Backup windows, maintenance, known flapping
   ports: the alarm is stored and shown under "Filtered as noise", nobody is notified.
   Each filter has an owner and, where possible, an end date.
8. **Measure it.** Reports show the noisiest assets and most frequent alerts; an alert that
   fires often and is always resolved without action gets a filter, a longer `for:` or is
   removed.

Methods behind these: the four golden signals (latency, traffic, errors, saturation) for
services, USE (utilisation, saturation, errors) for hosts and network devices.

## Review of `monitoring/alerts.yml` (November 2026)

| Finding | Change | When |
|---|---|---|
| Every rule was `warning`, including `NodeExporterDown`, `WindowsExporterDown`, `SnmpDeviceUnreachable` | Host-down rules are `critical` | done |
| Disk rules used only a static 90% | `NodeFilesystemFillingUp` / `WindowsFilesystemFillingUp` (`high`): full within 6 h at the last 6 h rate, only above 70% used, `for: 30m` | done |
| CPU and memory are pages | Keep as `warning` with `for: 10m`; they are evidence for ForgeRCA more than pages | Phase 3 |
| Demo rules fire after 10 s | Fine: demo only (`forgesre_demo_*`) | — |
| Backup windows, short spikes, hosts behind a down switch | Noise filters (below); parent-down suppression already existed | done |

CI checks every change to `monitoring/alerts.yml` with `promtool check rules` from the same
Prometheus image the appliance runs.

## Appliance checks: certificates, backups, heartbeats

Core runs these every 15 minutes (`health_checks` job on the leader) and sends the results
through the same intake as Alertmanager, so rules, noise filters, maintenance and escalation
apply. Labels: `job=forgesre-checks`, `check`, `target`, `asset`.

| Alert | Fires when | Severity |
|---|---|---|
| `TlsCertificateExpiring` | a `checks.tls_targets` endpoint's certificate expires in under `tls_warn_days` (14) / `tls_critical_days` (3) days; handshake failure | warning / critical / high |
| `ForgeSreBackupMissing` | the newest ForgeSRE backup is older than `backup_max_age_hours` (36), or there is none | critical |
| `HeartbeatMissing` | a heartbeat has not pinged within its `every_hours` plus 10 % | critical |
| `AssetLifecycleExpiring` | an asset's support / warranty end (its own lead days), end of life or licence date (`lifecycle_warn_days`, 30) is near or past; labels `lifecycle=support\|eol\|license` | warning; expired licence high |

Certificates are read without verification on purpose: an internal CA or a self-signed
certificate still expires. A heartbeat is a dead-man switch for anything ForgeSRE cannot see
(Veeam, rsync, database dumps): the job calls `POST /api/v1/heartbeats/{name}?every_hours=24&asset=nas-01`
with the `CHANGES_WEBHOOK_TOKEN` bearer after each success; the first call creates it. Results:
Settings › Checks, `GET /api/v1/checks`, `./forgesre checks`, and the **Checks** row in
`./forgesre doctor`.

## Noise filters

Automation › Noise filters (Engineer+), API `/api/v1/noise-filters`. A filter matches on
alert names (empty = any), scope (all, one asset, asset type, site), optional "only below"
value, and either:

- **Record, notify nobody** during given weekdays and a time window (appliance time zone;
  23:00–03:00 crosses midnight), optionally until an end date; or
- **Wait before opening**: the first N minutes of each alarm are quiet, so a spike that
  clears on its own never pages.

A matching alarm opens the incident as `SUPPRESSED` with a timeline line naming the filter;
the filter's hit count goes up. Every 30 s the leader re-checks quiet incidents: when the
filter no longer applies and the alarm is still firing, the incident opens and escalation
starts. Removing or disabling a filter does the same. "Try an alarm" on the Automation page
shows which filter would catch an alarm. An always-on quiet filter must be confirmed.

## Alarm storms, flapping and parents

Three things keep one root cause from paging people many times. All three keep the incidents
visible; they only stop their own mail ladder.

- **Parent down** (app.dc_noc): a down-style alert on a host whose parent, grandparent… (up to 6
  levels of `parent`) has an open down incident becomes **SUPPRESSED** and is linked to it
  ("caused by"). It pages again on its own once the parent is fixed and it still fires.
- **Alarm storm** (app.alarm_noise, Settings › Alarms): `alerts.storm.threshold` new incidents
  (default 20) within `alerts.storm.window_minutes` (5) open one CRITICAL **Alarm storm**
  incident that pages once. The incidents of that window and every new one while the storm is
  open link to it and do not page. Resolve the storm incident when the shared cause is found.
- **Flapping**: an alert that opened `alerts.flapping.threshold` incidents (3) within
  `alerts.flapping.window_minutes` (60) keeps opening incidents, shown with **flapping ×N**, but
  stops paging.

**Alert quality** (Reports, `GET /api/v1/reports/alert-quality?days=30`,
`./forgesre alert-quality 30`): per alert name, how many incidents, how many a person
acknowledged, how many cleared by themselves within 10 minutes, flapping and a suggestion (add a
noise filter, a longer `for:`, a higher threshold, or keep).

## Routing and traffic

Routers, firewalls and *Network device* assets (and any SNMP asset with extras
`snmp_routing=on`; `off` opts one out) are also polled with SNMP module `routing` (BGP4-MIB
peers, OSPF-MIB neighbours) by job `forgesre-snmp-routing`. Group `forgesre-routing`:

| Alert | Fires when | Severity |
|---|---|---|
| `BgpPeerDown` | an enabled BGP peer is not established for 5 min | high |
| `OspfNeighbourLost` | fewer OSPF neighbours in 2-way/full than at any time in the last hour, for 5 min | high |

NOC › *Network* and `./forgesre network` show BGP/OSPF sessions that are not up and the ten
busiest interfaces in and out (if_mib, 5 min rate). Flow data (NetFlow / sFlow top talkers)
needs a flow collector and is not part of ForgeSRE.

## Synthetic checks

Settings › Checks › *Synthetic checks* (Engineer) probes services from the appliance the way a
user would: **HTTP(S)** (2xx after redirects; certificate not verified), **TCP** port open,
**DNS** server answers (a query for `localhost`, so nothing leaves the DC) and **ping**.
blackbox_exporter (`blackbox-exporter`, 127.0.0.1:9115, `monitoring/blackbox.yml`) runs the
probes; Prometheus job `forgesre-synthetic` reads the checks through HTTP SD every 30 s and
probes each target every 60 s. Rules in group `forgesre-synthetic`:

| Alert | Fires when | Severity |
|---|---|---|
| `SyntheticCheckFailed` | a check marked critical fails for 3 min | critical |
| `SyntheticCheckDegraded` | a check marked warning fails for 10 min | warning |

Give the check an **asset** so the incident lands on that asset (otherwise the check name is
the asset label). `./forgesre synthetic` shows the last result per check; doctor row *Synthetic
checks (blackbox)*.

## Power and environment

UPS, PDU and environment sensors are polled over SNMP by Prometheus job `forgesre-snmp-power`
(module `power` in `monitoring/snmp.yml`). Rules are in group `forgesre-power`:

| Alert | Fires when | Severity |
|---|---|---|
| `UpsOnBattery` | `upsOutputSource` is 5 (battery) for 1 min | critical |
| `UpsBatteryLow` | battery status low/depleted, or under 10 minutes of runtime, for 1 min | critical |
| `UpsLoadHigh` | output load over 80% for 15 min | warning |
| `PowerTemperatureHigh` | an ENTITY-SENSOR celsius sensor over 32 °C for 10 min | high |
| `PowerSnmpUnreachable` | the power job cannot read the device for 5 min | high |

The NOC panel uses the same numbers (warning from 32 °C, red from 40 °C, load warning from
80%). Devices that do not implement UPS-MIB or ENTITY-SENSOR-MIB (some vendor PDUs) need a
vendor module; add it with SNMP onboarding (Inventory › SNMP › extra OIDs).

## Runbook format

Playbooks (and runbooks linked from an asset) use one shape, so operators and ForgeAI read
them the same way:

```text
Title:        what is wrong, in the operator's words ("Host unreachable")
Applies to:   alert names and asset groups
Impact:       what users notice
Check:        numbered read-only steps, each with the exact command or page
Fix:          numbered steps; mark any step that changes something with [CHANGE]
Roll back:    how to undo each [CHANGE] step
Escalate to:  team and level when the fix does not work within N minutes
Learned:      links to past incidents with the confirmed cause
```

Later, ForgeAI drafts a runbook from incidents with the same confirmed cause; an Engineer
reviews and saves it. It never runs a step.
