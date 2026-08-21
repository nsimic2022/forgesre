# Session handoff — 21 August 2026

This file is a **session handoff for the next coding agent or contributor**. It is not an operator manual. Operators start at [install and config](install-config.md) and the [operator handbook](operator-handbook.md).

Product on `main` at the end of this session: **V0.7**. Repository: https://github.com/nsimic2022/forgesre.

1. [Who and when](#1-who-and-when)
2. [Why this session existed](#2-why-this-session-existed)
3. [What shipped on main](#3-what-shipped-on-main)
4. [Product facts not to redo](#4-product-facts-not-to-redo)
5. [How to continue next session](#5-how-to-continue-next-session)
6. [Out of scope](#6-out-of-scope)
7. [Optional leftovers](#7-optional-leftovers)

---

## 1. Who and when

**Friday 21 August 2026.** Operator N ran a cloud-agent coding session against this repository. All of the work described below is merged to **`main`**. Do not treat unmerged feature branches as shipped.

On the Ubuntu VM N uses, resume with:

```bash
git pull origin main
./forgesre update
```

**Never** re-run `./install.sh` on a live box. That regenerates passwords in `secrets/secrets.env` and will wipe the install admin the operator already uses.

---

## 2. Why this session existed

N sent Ubuntu VM terminal screenshots of a real install, configuration, and debug CLI session. The request was to put that path into the docs, tidy install and config, and add a live tester that writes a detailed report (`./forgesre test`, beside `./forgesre doctor`). Titles in the docs should stay professional (**Advanced CLI**, not informal labels).

Later in the same day N asked for:

1. A detailed local-LLM implementation guide.
2. Documented `help quit` so operators can leave the `forgesre>` prompt.
3. Documented **wget of a Qwen3-4B GGUF** as the lab path on about 8 GB RAM.

Those three follow-ups are on `main`. They are not a rewrite of Core, Compose, or the RCA engine.

---

## 3. What shipped on main

Verified against `git log origin/main` on 21 August 2026. Do not claim work that is still only on a feature branch.

| Merge on `main` | Branch | Subject |
|---|---|---|
| `48fc1f158871925626c099f8bbb618c85325fe0b` | `cursor/appliance-test-docs-05f8` | Appliance test CLI + local LLM implementation guide (`53fe90d`) |
| `96e25bc969b45eddcbf5b61bdcafec07aaa4099a` | `cursor/llm-debug-cli-05f8` | CLI quit help + LLM debug CLI (`dfa555f`) |
| `0249562b7f407e67b3b8e02d40b929ac45ac0807` | `cursor/llm-wget-gguf-05f8` | wget of a lab Qwen3-4B GGUF (`deedadf`) |

### 3.1 Appliance test

Live verification on an installed VM:

```bash
./forgesre test
./test.sh
```

Implementation: `scripts/appliance_test.py` (root `./test.sh` and `scripts/test.sh` exec that script). It writes Markdown and JSON under `data/reports/` (`forgesre-test-<timestamp>.md` / `.json`; `data/` is gitignored). Exit code is **1 only when a check is FAIL**. WARN and SKIP do not fail the process. The script **does not send mail**, **does not run `./install.sh`**, and **does not change config**.

How to read a report: [`docs/verify.md`](verify.md). TAB completes `test` (and `help test`).

`./forgesre doctor` remains the short health light (same as System Health in the UI). pytest on a laptop is not a substitute for `./forgesre test`, and the appliance test is not a substitute for pytest.

### 3.2 Docs rewritten

These pages now match the real Ubuntu / vCenter path N used:

- [`docs/install-config.md`](install-config.md) — host `apt` / Docker / clone, `open-vm-tools`, **Advanced CLI**, verify with `./forgesre test`.
- [`docs/cli.md`](cli.md) — everyday commands plus Advanced CLI (git pull, rebuild Core, LLM container, VMware guest tools).
- [`docs/operator-handbook.md`](operator-handbook.md) — how to operate the product after install (users, servers, playrules, incidents, email, RCA).
- GitHub [`README.md`](../README.md) and [`docs/README.md`](README.md) index.

### 3.3 Local LLM

Full guide: [`docs/llm.md`](llm.md).

**ForgeRCA** (Python, `agents/rca/`) always investigates first. **ForgeAI** only rewrites prose (summary, likely cause, recommended action, extra limitations). The model does not SSH, run playbooks, or write NetBox.

Default `./forgesre fetch-llm` downloads **Qwen2.5-14B-Instruct Q4_K_M** (~9 GB) into `$FORGESRE_DATA/models/model.gguf` (default `./data/models/model.gguf`). That needs about **16 GB RAM** with the rest of the stack.

**Lab / about 8 GB RAM** — wget Qwen3-4B Q4_K_M to **`data/models/model.gguf`** (not the clone root), then enable the profile without regenerating secrets:

```bash
mkdir -p data/models
wget -O data/models/model.gguf \
  https://huggingface.co/Qwen/Qwen3-4B-GGUF/resolve/main/Qwen3-4B-Q4_K_M.gguf
ls -lah data/models/model.gguf
./forgesre fetch-llm --offline
```

A 4B GGUF sitting at that path is **not** the wrong model for a lab VM. `fetch-llm` will not overwrite a file already larger than 1 GB with the default 14B download.

Core reads **`config/forgesre.yml`**, not `config/forgesre.example.yml`. Compose profile **`ai`**, llama.cpp on **`127.0.0.1:8088`**. Smoke with `curl /v1/models` and `curl /health`. Do **not** run `docker compose down` to “fix” LLM — that stops the whole appliance.

### 3.4 CLI quit

```bash
./forgesre help quit
```

Leave the `forgesre>` prompt with `quit`, `exit`, or Ctrl-D. From host bash, `./forgesre quit` only prints that help.

---

## 4. Product facts not to redo

These already work on `main`. Do not “fix” them unless N asks.

- Incident ids look like `INC-0134_16.08.2026_09:13` (sequence + local date/time). Older `INC-000012` rows stay valid. TAB completes those ids after `incidents` / `history`.
- RCA is Python under `agents/rca/`. The LLM only rewrites prose. Builtin ForgeRCA always runs first.
- Secrets are gitignored (`secrets/`, `.env`, `config/forgesre.yml`, `data/`). Background jobs live in **Postgres** (`FOR UPDATE SKIP LOCKED`), not Redis/Celery.
- Core is an SMTP **client** only. The UI has no IMAP inbox. Humans read replies in Gmail, Outlook, or (later) Roundcube.
- Gmail and Outlook / Microsoft 365 are the supported send path now. Compose profile `mailbox` stays **off** until `./forgesre mailbox`. That command must **not** rewrite Core SMTP unless `--bind-core`.
- UI users are bcrypt hashes in Postgres. Administration is click-to-edit. You cannot delete yourself or the install `super_admin`.
- After UI / CSS changes, operators need a **hard refresh** in the browser (`/static/app.css` has no cache-busting query).

---

## 5. How to continue next session

1. `git pull origin main` (or fetch and check out `main`). Code lives there.
2. Read **this file**, then [`docs/llm.md`](llm.md) and [`docs/cli.md`](cli.md).
3. On the VM N uses: `git pull origin main && ./forgesre update`, then `./forgesre test`. Never `./install.sh` on that box.
4. Developer checks: `PYTHONPATH=backend:agents pytest tests` **twice**, then merge to `main`. New work uses branch pattern `cursor/<name>-05f8`.
5. Replies to N are in **Serbian**. OSS docs and code stay in **English**.
6. `ManagePullRequest` `update_pr` often fails with “PR URL must belong to the current repository”. `git merge` plus `git push origin main` still lands the change. Prefer that over fighting the PR updater.

---

## 6. Out of scope

Do not start these unless N asks:

- Do **not** implement the longer-term Go / Kubernetes rewrite described in [`docs/architecture.md`](architecture.md). That document is a design note, not a sprint.
- Do **not** change the default `./forgesre fetch-llm` URL from Qwen2.5-14B-Instruct Q4_K_M to 4B unless N asks.
- Do **not** enable Compose profile `mailbox` by default.
- Do **not** put a GGUF (or any model weights) in git.

---

## 7. Optional leftovers

These are documentation choices, not holes to fill on sight:

- `curl /health` is documented in [`docs/llm.md`](llm.md). It is not duplicated in every Advanced CLI list (`docs/install-config.md` §13 points at llm.md §8).
- The GitHub README `fetch-llm` line does not inline the 8 GB wget. The lab wget lives in [`docs/llm.md`](llm.md) §3.C.
