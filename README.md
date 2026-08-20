# ForgeSRE

Offline-first, self-hosted SRE platform for physical data-center infrastructure.

ForgeSRE does not replace Prometheus, Grafana, Loki, or NetBox. It connects discovery, inventory, monitoring, logs, incidents, read-only AI RCA, playrules, playbooks, and escalation into one system that a human can install, understand, and operate.

**This repository is in Phase 0.** The first deliverable is the architecture proposal, not a running stack.

- Architecture: [`docs/architecture.md`](docs/architecture.md)
- Central config contract: [`config/forgesre.example.yml`](config/forgesre.example.yml)
- Example playrule / playbook / escalation: [`config/examples/`](config/examples/)

## What V1 is for

Physical servers, network devices, storage, disks, filesystems, NICs, hardware health, availability, and infrastructure logs.

## What V1 is not

Application performance monitoring, Kubernetes, distributed tracing, microservice observability, or automatic remediation. The AI layer can observe and recommend. It cannot change infrastructure.

## Design rules

- Offline-first and self-hosted
- Bundled or external for Prometheus, Loki, Grafana, NetBox, and the LLM
- One operator config file, separate secrets, small `.env`
- Minimal, Standard, and Full AI deployment profiles
- Guided install and `forgesre doctor`, not “run compose and guess”

## Next step

Review [`docs/architecture.md`](docs/architecture.md). Implementation starts only after that proposal is accepted.
