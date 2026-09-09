# Session handoff — 9 September 2026

This file is a **session handoff for the next coding agent or contributor**. It is not an operator manual. Operators start at [install and config](install-config.md) and the [operator handbook](operator-handbook.md).

Product on `main`: **V0.7**. Repository: https://github.com/nsimic2022/forgesre.

1. [Who and when](#1-who-and-when)
2. [Checked twice (pytest)](#2-checked-twice-pytest)
3. [Done today / on this branch](#3-done-today--on-this-branch)
4. [What N should do on the VM](#4-what-n-should-do-on-the-vm)
5. [Product facts not to redo](#5-product-facts-not-to-redo)
6. [How to continue next session](#6-how-to-continue-next-session)

---

## 1. Who and when

**Wednesday 9 September 2026.** Operator N asked for `#` comments on **every variable**, **grouped by service** (`# --- Postgres ---`, `# --- NetBox ---`, `# --- Grafana ---`, … — not mixed “Ports” / “Compose mirrors” blocks), for both `.env` and `secrets/secrets.env` patterns, plus docs (where files live, how to edit passwords, plaintext vs bcrypt, install copies from examples). English comments in examples (OSS docs English). Two files stay separate — do not merge.

**Never** re-run `./install.sh` on a live box. That regenerates passwords in `secrets/secrets.env`. Never print tokens. Never commit real secrets.

Branches: `cursor/env-comments-docs-05f8` (initial comments + docs + install copy; `d2db0c1`, `fd2133c`, `06e6eac`, …) then `cursor/env-service-groups-76fa` (`cf90f9e` handoff + tighter tests). Latest `--no-ff` merge to `main`: `299eaec`. Prefer `git merge --no-ff` when PR create is 403.

---

## 2. Checked twice (pytest)

```bash
PYTHONPATH=backend:agents python3 -m pytest tests/test_netbox_compose.py tests/test_hardening.py
PYTHONPATH=backend:agents python3 -m pytest tests/test_netbox_compose.py tests/test_hardening.py
```

Focused compose/docs assertions for examples + gitignore must stay green. Full suite not required for example-comment-only edits when prior `main` was green. (Some hardening tests may fail independently of this docs change — fix on their own branch if they regress.)

`create_pr` / `ManagePullRequest` often **403** — `git merge --no-ff` plus `git push origin main` still lands the change.

---

## 3. Done today / on this branch

- [`.env.example`](../.env.example) — English `#` comments; **service groups**: Appliance, Compose profiles, Postgres, Grafana, NetBox, Generated monitoring, Optional mailbox. Header states where the live file lives, install/`cp` from example, plaintext Compose mirrors vs bcrypt UI hashes, and “do not merge with secrets”.
- [`secrets/secrets.example.env`](../secrets/secrets.example.env) — same pattern: Postgres, ForgeSRE Core, Grafana, SMTP, SNMP, NetBox, Optional mailbox. All values **plaintext**; UI bcrypt only in Postgres after first-boot seed (`backend/app/seed.py` only creates the admin when the email is missing).
- [`.gitignore`](../.gitignore) — still ignores `secrets/secrets.env` and `secrets/*.env`, with `!secrets/secrets.example.env` so the example is tracked.
- [`scripts/install.sh`](../scripts/install.sh) — copies examples → live `.env` / `secrets/secrets.env`, then `set_kv` fills passwords (comments preserved).
- Docs: [`install-config.md`](install-config.md) §§8/10/11/11a tables match service groups; plaintext vs bcrypt; [`operator-handbook.md`](operator-handbook.md) “Where passwords live”; [`README.md`](README.md) index. Changing `FORGESRE_ADMIN_PASSWORD` after first boot does **not** update the DB — documented truthfully.
- Tests assert service headers and that Postgres/Grafana/NetBox keys sit under those headers (no `# --- Ports ---`).

---

## 4. What N should do on the VM

Do **not** run `./install.sh`.

```bash
git pull origin main && ./forgesre update
```

To inspect templates only: `.env.example` and `secrets/secrets.example.env` in the clone. Live secrets stay in `secrets/secrets.env` (mode `600`). To rotate container secrets: edit `secrets/secrets.env` → `docker compose up -d --force-recreate …` as in install-config §11a. UI password changes: Administration (bcrypt in DB), then optionally sync `FORGESRE_ADMIN_PASSWORD` for CLI fallback.

NetBox: keep the **full v2 token** (shown once at create, not the 12-character key) in `NETBOX_API_TOKEN`. Recreate **core** only if secrets changed. Never print the token.

```bash
docker compose logs netbox | grep forgesre
```

Expect **`skipping v1 upsert`** (v2 already in secrets) or **`v1 token ready`** (legacy 40-char). Not **`could not upsert`** for a valid v2 secret.

---

## 5. Product facts not to redo

- Two files: `.env` (deployment at repo root) vs `secrets/secrets.env` (secrets). Do not merge.
- Example comments stay **English** and **grouped by service**.
- `secrets.env` values are **plaintext** env for containers. Do not claim they are hashes.
- UI users: bcrypt in `users.password_hash` only. Seed uses `FORGESRE_ADMIN_PASSWORD` once.
- Bundled **NetBox** is a **default** compose service (`:8001`). Do not put it behind a profile. Prefer a NetBox UI **v2** token (full `nbt_…`) in `NETBOX_API_TOKEN`; Core sends `Authorization: Bearer`. `scripts/netbox-upsert-token.py` **skips** v1 create for that secret. A 40-character **v1 plaintext** value is still upserted as fallback (`write_enabled=False`) on the **superuser**. Success log: **`skipping v1 upsert`** or **`v1 token ready`**. **`could not upsert`** means a v1 secret never landed in the NetBox DB (UI still starts; Discovery 403 is honest). Never touch `User.is_staff` (removed in NetBox 4.5). Core is **not** a NetBox UI login. Do not drop database `forgesre` to “fix” NetBox. Health NetBox tile: UI up + 403 = **warn**, not paused. HTTP 200 with v2 is yellow (0 devices) / green (≥1).
- NetBox UI **API token peppers**: v4.5+ needs `API_TOKEN_PEPPERS` / `NETBOX_API_TOKEN_PEPPER`. Official image reads `API_TOKEN_PEPPER_1`.
- Discovery **Sync NetBox**: Devices API 403 is a warning (rejected / recreate), not “token missing” when a secret is present. Status chip: Not connected / API 403 / No devices / Connected — grey/yellow/green. Sentence *No devices yet; add in NetBox UI `:8001` or use Assets/Discovery.* stays on yellow. **Admin only** for engineer/analyst; viewers cannot open `/discovery`.
- SNMP with no Network device + IP is **paused (no SNMP targets)** (yellow — OK).

---

## 6. How to continue next session

1. `git pull origin main`.
2. Read **this file**, then the [docs index](README.md).
3. On the VM: `git pull origin main && ./forgesre update`. Keep the full v2 token in `NETBOX_API_TOKEN`. `docker compose logs netbox | grep forgesre` should say **skipping v1 upsert** (v2) or **v1 token ready** (fallback), not could not upsert for a valid v2 secret. Never `./install.sh`. Never print tokens.
4. Branch pattern `cursor/<name>-05f8` (or task suffix). Prefer `git merge --no-ff` to `main` when PR create is 403. Replies to N are in **Serbian**. OSS docs and code stay in **English**.
