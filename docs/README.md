# ForgeSRE documentation

Start with the **guide**: six short chapters, one per job, with screenshots. The **reference**
pages below go deeper when you need every setting or every command.

## Guide

| | Chapter | Read it when you |
|---|---|---|
| 1 | [Getting started](guide/01-getting-started.md) | install ForgeSRE, sign in for the first time, try the demo |
| 2 | [A day on the NOC](guide/02-daily-work.md) | watch the NOC, take an incident from alarm to postmortem, run the wall screen |
| 3 | [Assets and dependencies](guide/03-assets.md) | add servers and devices, build the dependency map, plan maintenance |
| 4 | [Alarms, noise and escalation](guide/04-alerts-and-noise.md) | decide what becomes an incident, who gets called and when |
| 5 | [Reports and SLOs](guide/05-reports-and-slo.md) | report to management, track availability and error budgets |
| 6 | [Running the appliance](guide/06-admin.md) | manage users, settings, backups, updates, health and integrations |

Screenshots are taken with `scripts/screenshots.py` against a demo install, so they can be
refreshed after a UI change.

## Reference

- [Vodič na srpskom: instalacija, `.env`, tajne, integracije, ForgeAI i podešavanje modela](vodic-sr.md)

- [Install and config](install-config.md): Ubuntu VM, ports, `.env`, `secrets/secrets.env`, every secret
- [Operator handbook](operator-handbook.md): the long manual, section by section
- [CLI](cli.md): every `./forgesre` command
- [Settings](settings.md): the Settings page and `./forgesre settings`
- [Sign-in with LDAP / Active Directory or OIDC single sign-on](sign-in.md)
- [Appliance without internet (offline bundle)](offline.md)
- [High availability: two appliances on one Postgres](high-availability.md)
- [Discovery: devices come from LibreNMS](discovery.md)
- [How the classic UI was retired](classic-gaps.md)
- [Alerting](alerting.md): alert rules, playrules, noise filters, appliance checks, runbook format
- [New UI](ui.md): pages, API endpoints, TV mode, dependency map, postmortem
- [Architecture](architecture.md): containers, the alarm path, background jobs
- [Local LLM (ForgeAI)](llm.md)
- [Kubernetes, OpenShift, k3s](kubernetes.md)
- [Read-only MCP server for AI clients](mcp.md)
- [Zabbix (read-only)](zabbix.md) · [GLPI (read-only)](glpi.md)
- [Verify the appliance](verify.md)
- [Roadmap](roadmap.md)

Release notes: [V0.1](v0.1.md) · [V0.2](v0.2.md) · [V0.3](v0.3.md) · [V0.4](v0.4.md) · [V0.5](v0.5.md) ·
[V0.6](v0.6.md) · [V0.7](v0.7.md) · [V0.8](v0.8.md) · [V0.9](v0.9.md) · [V2.0](v2.0.md) (current)

## For developers

How code is written: [AGENTS.md](../AGENTS.md) and [CONTRIBUTING.md](../CONTRIBUTING.md).
Design notes: [V0.3 implementation plan](V03_IMPLEMENTATION_PLAN.md). Session handoff for
the next contributor: [continuation.md](continuation.md). Feature audit: [audit/](audit/).
