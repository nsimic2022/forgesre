# Security

ForgeSRE is designed to be self-hosted on a management network.

## Secrets

- Never commit `secrets/secrets.env` or `.env`.
- Keep `secrets/` mode `0700` and `secrets.env` mode `0600`.
- Do not put passwords in `config/forgesre.yml`.

## AI

The investigation / ForgeRCA path is **read-only**. It receives data ForgeSRE already collected (Prometheus, Loki, inventory). Secrets are stripped before any LLM prompt. It has no SSH, Docker socket, or infrastructure write credentials. LLM output is never executed.

Any future remediation feature must be a separate, off-by-default, audited subsystem.

## Reporting

If you find a vulnerability, open a private report with the project maintainers. Do not file a public issue with exploit details.
