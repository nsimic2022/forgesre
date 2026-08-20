# Contributing

V0.1 is a small vertical slice. Please keep it that way.

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

```bash
python3 -m venv .venv
. .venv/bin/activate
pip install -r backend/requirements.txt
PYTHONPATH=backend:agents pytest tests
```

## Code style

Prefer readable Python over clever abstractions. New services need a design note in `docs/`.
