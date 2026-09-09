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

**Wednesday 9 September 2026.** N rejected hardcoded `/24` Discovery. Final design: **multi-CIDR autodetection of all connected IPv4 nets** with real `prefixlen` (not `/24`-only), combined with the Discovery **background job** queue (no `run_scan` on the HTTP thread). Branch: `cursor/discovery-autocidr-abbb`. Prefer `git merge --no-ff` when PR create is 403.

**Never** re-run `./install.sh` on a live box. That regenerates passwords in `secrets/secrets.env`. Never print tokens. Never commit real secrets.

---

## 2. Checked twice (pytest)

```bash
PYTHONPATH=backend:agents python3 -m pytest
PYTHONPATH=backend:agents python3 -m pytest
```

Count and final merge SHA are filled after the green runs and `--no-ff` merge below.

---

## 3. Done today / on this branch

### Discovery multi-CIDR + job queue

- Autodetect all connected IPv4 nets (real prefixes). Skip loopback / link-local / multicast / `0.0.0.0/0` and Docker bridges by default.
- Prefill form with connected ∪ YAML; operator edits; **Scan now** / **Save & scan** persist `discovery.cidrs` and **queue** a probe (Postgres jobs). Worker scans payload CIDRs with `merge_auto=False` (no silent re-merge).
- Limits 256/CIDR, 1024 total with UI truncate warnings. Candidate columns: open ports, node_exporter, windows_exporter, SNMP. Approve queue unchanged. Not nmap.
- Scan now and NetBox sync stay half-width side by side.
- No **Confirm & scan** / management-`/24` product copy.

---

## 4. What N should do on the VM

Do **not** run `./install.sh`.

```bash
git pull origin main && ./forgesre update
```

Hard-refresh the browser. Open **Discovery**: review prefilled connected CIDRs, edit if needed, **Scan now**. Candidates appear when the background job finishes. **Sync NetBox** sits beside Scan now. Approve / Ignore as before.

---

## 5. Product facts not to redo

- Discovery **Scan now** = multi-CIDR autodetection (real prefixes), not `/24`-only, not Confirm & scan, not nmap. Probe runs as a Postgres job (one Core worker — not Celery).
- Discovery candidate booleans migrate with `BOOLEAN DEFAULT FALSE`.
- Bundled NetBox `:8001` default; prefer full v2 `nbt_…` token. Core is not a NetBox UI login.
- SNMP with no Network device + IP is **paused (no SNMP targets)** (yellow — OK).

---

## 6. How to continue next session

1. `git pull origin main`.
2. Read **this file**, then the [docs index](README.md).
3. On the VM: `git pull origin main && ./forgesre update`. Never `./install.sh`. Never print tokens.
4. Prefer `git merge --no-ff` to `main` when PR create is 403. Replies to N are in **Serbian**. OSS docs and code stay in **English**.
