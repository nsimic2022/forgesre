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
| Every rule is `warning`, including `NodeExporterDown`, `WindowsExporterDown`, `SnmpDeviceUnreachable` | Host-down rules become `critical`; NOC sorting and escalation follow urgency | Phase 3, with noise filters |
| Disk rules use only a static 90% | Add `predict_linear(...[6h], 6*3600) < 0` "full within 6 h" as `high` | Phase 3 |
| CPU and memory are pages | Keep as `warning` with `for: 10m`; they are evidence for ForgeRCA more than pages | Phase 3 |
| Demo rules fire after 10 s | Fine: demo only (`forgesre_demo_*`) | — |
| Backup windows, short spikes, hosts behind a down switch | Noise filters (below); parent-down suppression already existed | done |

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
