# Rules for anyone changing this code (people, Cursor, Claude)

Read this before writing code. `CONTRIBUTING.md` covers why a component may be added;
this file covers how code is written.

## 1. Comments: short, in English, for the next developer

All comments and docstrings are in English, written for a developer who has to change
or fix the code.

- **Important functions get a docstring**: anything other modules call, anything that
  writes to the database, sends a message, calls another system or makes a decision
  (matching, routing, escalation, permissions). One or two lines is usually enough.
- **Skip the obvious**: a three-line helper whose name says it all needs no docstring.
- Say what it does and anything surprising: side effects, what happens on failure,
  why it is done this way. Do not repeat the code in words.
- Use a `#` comment inside the body only where the reason for a line is not obvious.
- Existing code gets comments module by module, when that module is reorganised.

```python
def match_playrule(db: Session, alertname: str, asset: Asset | None) -> Playrule | None:
    """Pick the playrule for an incoming alert: asset rules first, then global ones.

    Returns None when nothing matches; the incident still opens, without a playbook.
    """
```

## 2. Nothing may block the platform

Many people use the UI at once, and alarms keep arriving while a slow system answers.

- Every call to another system (Prometheus, Alertmanager, Loki, Zabbix, LibreNMS, GLPI,
  NetBox, SMTP, Slack/Teams/Discord, the LLM, `docker`, `ping`, SNMP) has a timeout.
- Work that can take more than about a second (ForgeAI, discovery, syncs, reports,
  backups) runs as a background job, never inside a web request or the alert webhook.
- A failing integration shows as an error on its Settings card and in the journal; it
  must not stop alert intake or the UI.
- State that must be shared between processes (sessions, throttles, job state) lives in
  the database, not in process memory, once Core runs more than one worker.

## 3. Roles

Four roles; each has everything the role before it has.

| Role | Can |
|---|---|
| Viewer | See NOC, incidents, inventory, reports (wall screen, management) |
| Analyst | NOC operator: acknowledge, assign, notes, playbook checklist, local ForgeAI |
| Engineer | Change everything operational: inventory, rules, noise filters, playbooks, escalation, on-call, reports, any team; re-run ForgeAI with a cloud model |
| Admin | Also settings, integrations, users, backup, import and export. Replaces the old admin and super admin; the last active Admin cannot be removed or demoted |

Check permissions on the server for every write, not only by hiding buttons.

## 4. The CLI and scripts stay in step with the platform

`./forgesre` (and `./f`), `install.sh`, `update.sh`, `backup.sh`, `restore.sh` and
`doctor.sh` are part of the product, not extras.

- A change that adds or moves a setting, a container, a port, a secret or a database
  column also updates install, update and doctor in the same pull request.
- Whatever an operator can see in the UI about health (services, integrations,
  queues, backups, incidents) can also be checked from `./forgesre`, so the platform
  can be run and debugged over SSH alone.
- `./forgesre doctor` stays the first command to run when something is wrong.

## 5. Before a pull request

- `pytest` passes; CI (`.github/workflows/ci.yml`) must be green before merge.
- New behaviour has a test. No absolute paths such as `/workspace/...` in tests: build
  paths from `Path(__file__)`.
- `scripts/gui_crawl.py` walks the UI; `tests/test_gui_crawl.py` must stay green.
- Work on a branch and open a pull request; never push to `main` directly.
- When tests pass locally and CI is green, merge the pull request without waiting.

## 6. Documentation

Docs are in English, short and aimed at the person who installs, runs or changes
ForgeSRE. Update the doc in the same pull request as the change it describes.
