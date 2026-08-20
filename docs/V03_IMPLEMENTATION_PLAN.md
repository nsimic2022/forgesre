# ForgeSRE V0.3 — Implementation plan

**Date:** 2026-08-20  
**Status:** plan for implementation on top of V0.1 + V0.2  
**Does not replace:** V0.1 incident/demo path, V0.2 discovery/HTTP SD

This document compares the V0.3 RCA specification with the running code and records what we will build, reuse, and refuse to over-build.

---

## 1. What already exists (reuse)

| Spec item | Current code | Verdict |
|---|---|---|
| Incident as RCA input | `Incident` + `POST /api/v1/incidents/{number}/investigate` | **Reuse** |
| Evidence store | `evidence` table (`kind`, `title`, `payload`) | **Extend** — add immutable RCA fields, do not create a second store |
| Evidence collection | `collect_evidence()` + Prometheus/Loki HTTP queries | **Extend** — preserve PromQL/LogQL, time window, per-item IDs |
| Investigation result | `investigations` (`summary`, `likely_cause`, `confidence`, `evidence[]` strings) | **Extend** — keep V0.1 columns; add structured `result` JSON |
| Builtin analyst | `agents/investigation.py` heuristic + optional OpenAI-compatible LLM | **Wrap** — keep the function signature for V0.1 tests; implementation becomes ForgeRCA |
| LLM endpoint | `ai.llm.url` llama.cpp / OpenAI-compatible | **Reuse** as one `LLMProvider` |
| Read-only AI | No SSH/Docker/credentials in the agent | **Keep** |
| Audit | `audit(..., action="ai.investigation")` | **Extend** payload (engine, model, evidence IDs) |
| Analyst vs engineer UI | Incident page + `/ai/{number}` + raw payload dump if `read_evidence` | **Extend** — facts/hypotheses/evidence chain; engineer still sees queries |
| Visual timeline | `incident.timeline` nodes | **Reuse** + add an RCA flow (ALERT→ANOMALY→EVIDENCE→HYPOTHESIS→ROOT CAUSE). Not a graph engine |
| Playrules/playbooks | Matched on ingest | **Feed into RCA context**. Still not executed by AI |
| Demo | `./forgesre demo` HighCPU | **Keep**. Add a filesystem RCA demo (`demo-rca`) that does not fill a real disk |

V0.1 tests (`investigate()` disclaimer, CPU summary, `builtin-analyst`) must keep passing.

---

## 2. Gaps the V0.3 spec actually needs

1. ForgeSRE-owned **RCA engine interface** (not “call investigation.py from five places”).
2. **Facts vs hypotheses vs anomalies** as first-class fields.
3. **Immutable evidence items** with `EV-…` ids, source, original query, hash.
4. Deterministic **anomaly detection** before any LLM call.
5. Ranked **candidate causes** with supporting/contradicting evidence and a documented (not scientific) confidence score.
6. **LLM provider** abstraction + sanitization of secrets before prompt.
7. Maintenance windows in RCA context.
8. Failure isolation: missing Prometheus/Loki/LLM must degrade, not break ingest.
9. Filesystem demo that exercises the disk playbook path.

---

## 3. Disagreements with the spec (on purpose)

The spec is the product intent. These cuts keep V0.3 debuggable in Python and avoid a rewrite:

| Spec | V0.3 decision | Why |
|---|---|---|
| New evidence database | Same PostgreSQL `evidence` table + columns | One store to debug |
| Confidence as 0–1 only | `result.root_cause.confidence` is 0–1; `investigations.confidence` stays 0–100 for the existing UI | Do not break V0.1 screens |
| Separate Data/RCA/Report/Visual agents | **One in-process ForgeRCA**. Modules are functions, not services | V0.1 ADR: no agent microservices |
| Ollama SDK + OpenAI SDK | One OpenAI-compatible HTTP client (already covers llama.cpp, Ollama, vLLM) | Fewer libraries |
| Code Agent | **Not implemented** | Spec forbids it |
| Visual Agent graph | HTML flow of RCA nodes | Enough for “why did we conclude this?” |
| Maintenance calendar UI | Table + collector; no scheduler UI | RCA needs the data, not a CMDB |
| Real disk fill for demo | `forgesre_demo_disk_percent` gauge | Same trick as demo CPU; filling `/` is hostile |
| Vector/historical semantic search | Filter by asset + alertname | Spec allows this for V0.3 |

---

## 4. Target architecture (owned by ForgeSRE)

```
Incident
    → Evidence Collector (Prometheus, Loki, inventory, history, playrule, maintenance)
    → Evidence Set (immutable EV-* items)
    → RCA Context (facts-ready, sanitized copy for LLM)
    → RCAEngine.investigate()
           └─ ForgeRCA          (production, V0.3)
    → RCA Result
    → Investigation row + audit
    → Analyst UI / Engineer queries / Playbook (guidance only)
```

ForgeRCA pipeline:

```
RCA Context → deterministic analysis → candidate causes
           → optional LLM reasoning on sanitized context
           → validate LLM output (never execute)
           → score confidence in ForgeSRE, not “whatever the model said”
           → RCA Result
```

---

## 5. Database

**Alter `evidence`:** `evidence_id`, `source`, `query`, `asset_ref`, `hash`, `confidence`  
`kind` / `title` / `payload` stay (V0.1 rollup rows).

**Alter `investigations`:** `result` JSON, `engine`, `engine_version`, `model`, `requested_by`

**New `maintenance_windows`:** `asset_ref`, `summary`, `starts_at`, `ends_at`

No Alembic. Same `migrate()` helper as V0.2. `create_all` for new tables.

---

## 6. Code layout

| Path | Role |
|---|---|
| `agents/rca/` | RCA abstraction (collector, analysis, engines, LLM, sanitize) |
| `agents/investigation.py` | V0.1 wrapper around ForgeRCA |
| `backend/app/services.py` | Persist evidence + call engine (do not duplicate analysis) |
| `docs/v0.3.md` | Operator-facing RCA notes + confidence formula |
| `tests/test_v03.py` | Collector, normalize, anomalies, scoring, sanitize, ForgeRCA |

---

## 7. API (existing conventions)

Keep `POST /api/v1/incidents/{number}/investigate`.

Add:

- `GET /api/v1/incidents/{number}/investigation` — latest
- `GET /api/v1/investigations/{id}`
- `GET /api/v1/investigations/{id}/evidence`
- `POST /api/v1/demo-rca` — filesystem acceptance path

Analysts may request RCA (`read_ai`), matching the existing HTML form. Viewers do not. Queries/raw payloads stay behind `read_evidence`.

---

## 8. Confidence (honest, simple)

```
0.45 baseline
+ up to 0.20 for anomalies
+ up to 0.20 for supporting evidence count
− up to 0.15 for contradicting evidence
+ 0.08 if a similar historical incident exists
− 0.20 if the asset is in a maintenance window
− 0.15 if Prometheus or Loki was unavailable
clamp to [0.15, 0.95]
```

If an LLM is used and its cause family agrees with ForgeRCA, keep ForgeSRE’s score (do not replace it with the model’s number). If they disagree, keep the deterministic cause and add a limitation.

This is **not** a validated reliability model.

---

## 9. Implementation order

1. Plan (this file)
2. `agents/rca` + tests that do not need Docker
3. Migrate models; wire `run_investigation`
4. UI: facts / hypotheses / evidence chain / engineer queries
5. Filesystem `demo-rca` + config window
6. Docs, doctor unchanged (LLM already reported)
7. Keep V0.1/V0.2 tests green; e2e on the running stack

---

## 10. Out of scope (later)

Data Agent / Report Agent / Visual Agent as named services, sandboxed Code Agent, SNMP/traces/NetBox dependency graphs, executing playbooks.
