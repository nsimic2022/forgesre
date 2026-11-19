# Contributing

Thanks for helping. ForgeSRE (current product: V2.0) is a small appliance for people who run real data centers; every
new part has to earn its place. Please keep it that way.

## How to contribute

1. For anything bigger than a small fix, open an issue first so we can agree on the approach.
2. Fork, create a branch, make the change with a test, and open a pull request against `main`.
3. Fill in the pull-request checklist. CI (lint, tests, web build, alert rules) must be green.
4. Never include real IP addresses, hostnames, emails, tokens or customer data in issues,
   tests, screenshots or logs.

Good first areas: documentation, alert rules and runbooks (see [docs/alerting.md](docs/alerting.md)),
tests, translations of operator text, and items marked in [docs/roadmap.md](docs/roadmap.md).

## License of contributions

ForgeSRE is licensed under [Apache-2.0](LICENSE). By opening a pull request you agree that your
contribution is licensed under the same terms (Apache-2.0, section 5) and that you have the
right to submit it. Please sign off your commits (`git commit -s`) to confirm this.

## Before adding a component

Answer:

- Why?
- Can an existing component do it?
- Can ForgeSRE core do it?
- Can this be optional?
- What complexity and security risk does it add?

If there is no good answer, do not add it.

## Stack

- Python 3.12, FastAPI, SQLAlchemy (backend)
- Jinja2 templates (classic UI, `frontend/`)
- React + TypeScript + Vite (new UI, `web/`)
- Bash (install, doctor, backup, update)
- Docker Compose for runtime dependencies

## Tests

Developer unit tests (no live Docker stack):

```bash
python3 -m venv .venv
. .venv/bin/activate
pip install -r requirements-dev.txt
PYTHONPATH=backend:agents pytest tests
```

`requirements-dev.txt` adds pytest. The Core Docker image installs `backend/requirements.txt` only and must not include pytest.

New UI:

```bash
cd web && npm ci && npm run typecheck && npm test && npm run build
```

On an installed VM, `./forgesre test` is the live appliance report (`data/reports/`). It is not a substitute for pytest, and pytest does not replace `./forgesre test`.

## Code style

Prefer readable Python over clever abstractions. New services need a design note in `docs/`.

Docstrings, non-blocking calls, roles and pull-request rules: see [AGENTS.md](AGENTS.md).
