# OpenRCA evaluation (not production)

**Operators can skip this page.** It is a developer note. Nothing here is installed, enabled, or required to run ForgeSRE.

ForgeSRE V0.3 does **not** run Microsoft OpenRCA and does **not** ship the OpenRCA dataset.

Production RCA is `ForgeRCA` (`agents/rca/engines.py`). `OpenRCAAdapter` exists so a later evaluation harness can plug in without changing incident/evidence/result shapes.

## Why it is separate

- The dataset is a research/eval corpus, not something an air-gapped appliance should download at install time.
- OpenRCA’s runtime assumptions (offline traces, labeled root causes, batch scoring) are not the same as a live Prometheus/Loki incident.
- ForgeSRE must keep working if that project is unavailable.

## Later evaluation sketch

1. Keep using ForgeSRE evidence snapshots (`investigations.result` + immutable `EV-*` rows).
2. Export an RCA Context JSON (already `RCAContext.to_dict()`, secrets sanitized).
3. In a **dev** environment only, map that JSON into whatever OpenRCA expects.
4. Compare OpenRCA’s ranked causes with ForgeRCA hypotheses on the same snapshot.
5. Do not point the adapter at production Prometheus, Loki, NetBox, or SSH.

Do not add the dataset to `./install.sh` or the Docker image.

## OpenDeRisk

Same rule: reuse the ideas (evidence chain, report, visual investigation) as ForgeSRE modules later. Do not copy that architecture into V0.3 services. There is no OpenDeRisk adapter in this release.
