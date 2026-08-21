# Docs

The GitHub README is the product summary (what ForgeSRE is, what it is not, install). This folder is the operator manuals and release notes.

## For operators

How to install and run ForgeSRE:

- [Install and config (Ubuntu / vCenter)](install-config.md)
- [Operator handbook (users, servers, playrules, incidents, email)](operator-handbook.md)
- [Verify the appliance](verify.md) (`./forgesre doctor` vs `./forgesre test`)
- [Operator CLI (everyday + advanced)](cli.md)
- [Local LLM (ForgeAI / llama.cpp)](llm.md)

What each release actually shipped (read if you want the “why”, not required to operate):

- [V0.1 implementation](v0.1.md)
- [V0.2 discovery and inventory](v0.2.md)
- [V0.3 RCA foundation](v0.3.md)
- [V0.4 asset contacts, analyst inventory, first-hour demo](v0.4.md)
- [V0.5 bundled snmp_exporter](v0.5.md)
- [V0.6 operations hardening](v0.6.md)
- [V0.7 incident history](v0.7.md)

## For developers (not used in production)

These are design notes. You do **not** install or enable them on the VM.

- [Architecture (longer-term)](architecture.md)
- [V0.3 implementation plan](V03_IMPLEMENTATION_PLAN.md)

**Session handoff** (next coding agent / contributor, not an operator start page): [continuation.md](continuation.md).
