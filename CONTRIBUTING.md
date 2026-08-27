# Contributing

V0.7 is still a small vertical slice. Please keep it that way.

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
- Jinja2 templates (frontend)
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

On an installed VM, `./forgesre test` is the live appliance report (`data/reports/`). It is not a substitute for pytest, and pytest does not replace `./forgesre test`.

## Code style

Prefer readable Python over clever abstractions. New services need a design note in `docs/`.
