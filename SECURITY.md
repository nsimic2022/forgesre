# Security

ForgeSRE is designed to be self-hosted on a management network.

## Secrets

- Never commit `secrets/secrets.env` or `.env`.
- Keep `secrets/` mode `0700` and `secrets.env` mode `0600`.
- Do not put passwords in `config/forgesre.yml`.
- Core **refuses to start** if `SECRET_KEY` or `ALERTMANAGER_WEBHOOK_TOKEN` is still a shipped default (`forgesre-dev-secret-change-me`, `forgesre-dev-webhook-token`, `CHANGE-ME-RENDER-MONITORING`) unless `FORGESRE_DEV=1` (unit tests / explicit throwaway lab). Check with `./forgesre secrets-check`.
- Backups are tar mode `600` under `data/backups/` (gitignored). Use `./forgesre backup --no-secrets` when the archive will leave the VM. Administration download requires an admin session (`Cache-Control: no-store`). Never commit backups.
- There is **no browser PTY**. A web terminal wrapped around `./forgesre` would still be a host command channel (XSS / stolen admin cookie). Use SSH + `./forgesre shell` on the box. See `docs/operator-handbook.md` (Administration → Appliance shell).
- Session cookie `Secure` only when `system.cookie_secure` or `FORGESRE_COOKIE_SECURE=1` (HTTPS). Lab default is HTTP on the management VLAN.
- `/api/v1/system/doctor` requires a login session or `Authorization: Bearer` with the webhook token. Prometheus HTTP SD uses the same token.

## AI

The investigation / ForgeRCA path is **read-only**. It receives data ForgeSRE already collected (Prometheus, Loki, inventory). Secrets are stripped before any LLM prompt. It has no SSH, Docker socket, or infrastructure write credentials. LLM output is never executed. Demo gauges are applied only for `forge-demo-01`.

Any future remediation feature must be a separate, off-by-default, audited subsystem.

## Reporting

If you find a vulnerability, open a private report with the project maintainers. Do not file a public issue with exploit details.
