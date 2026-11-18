# Rules for anyone changing this code (people, Cursor, Claude)

Read this before writing code. `CONTRIBUTING.md` covers why a component may be added;
this file covers how code is written.

## 1. Every function says what it does

Every new or changed module, class and function gets a docstring in English, right
under the `def` / `class` line. Hovering the name in VS Code or Cursor shows it.

```python
def match_playrule(db: Session, alertname: str, asset: Asset | None) -> Playrule | None:
    """Pick the playrule for an incoming alert.

    First enabled rule whose alertname matches wins; asset-specific rules are tried
    before global ones. Returns None when no rule matches (the incident still opens,
    with no playbook).
    """
```

- First line: what it does, in one sentence.
- Then, when it is not obvious: inputs that matter, what it returns, side effects
  (writes to the database, sends mail, calls a container, starts a subprocess), and
  what happens on failure.
- Use a `#` comment inside the body for a line whose reason is not obvious from the code.
- Existing code without docstrings gets them module by module, when that module is
  reorganised. Do not open a pull request that only adds docstrings to code that is
  about to move.

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

One ladder; each role has everything the role before it has.

| Role | Can |
|---|---|
| Viewer | See NOC, incidents, inventory, reports (wall screen, management) |
| Analyst | NOC operator: acknowledge, assign, notes, playbook checklist, local ForgeAI |
| Engineer | Edit inventory, rules, noise filters, playbooks, escalation across all teams; re-run ForgeAI with a cloud model |
| Admin | Settings, integrations, users, backup, import and export |
| Super admin | Install owner; cannot be deleted or demoted |

Check permissions on the server for every write, not only by hiding buttons.

## 4. Before a pull request

- `pytest` passes; CI (`.github/workflows/ci.yml`) must be green before merge.
- New behaviour has a test. No absolute paths such as `/workspace/...` in tests: build
  paths from `Path(__file__)`.
- `scripts/gui_crawl.py` walks the UI; `tests/test_gui_crawl.py` must stay green.
- Work on a branch, open a pull request, never push to `main` directly.
