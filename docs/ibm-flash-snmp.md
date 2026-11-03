# IBM Flash over SNMP — what ForgeSRE actually does

ForgeSRE can **own an IBM Flash / FlashSystem / Storwize box as an asset** and poll it with the bundled **snmp_exporter**. That path is real. It is **not** an IBM storage collector.

There is **no IBM MIB**, no pool / vdisk / enclosure / battery series, and no `StorageVolumeUsageHigh` Prometheus rule. The walk is the same **IF-MIB slice** used for every Switch / Storage / Printer: `sysUpTime`, `ifAdminStatus`, `ifOperStatus`, `ifHCInOctets`, `ifHCOutOctets`. The asset page says so: *No extra vendor MIB was added.*

This file is the honesty report for “can I watch IBM Flash with SNMP here?”

---

## Short answers

| Question | Answer |
|---|---|
| Where do I put it? | **Assets → Add**. Type **Storage**. IP = the **management** address that answers SNMP. Leave scrape address empty. |
| Can I only use SNMP? | Yes. That is the Storage path. No node_exporter, no IBM REST. |
| Will I get graphs? | **NET RX/TX** (interface octets) and **Collecting** (`up`) if the walk succeeds. **Not** CPU / RAM / array capacity. |
| Is that IBM health? | **No.** It is ethernet IF-MIB on the controller. Same as a switch. |
| Is the SNMP path fake? | **No** for reachability + interfaces. **Yes** if you expect IBM Flash metrics. `ifHighSpeed` is queried in PromQL but **not walked** — the speed sample stays empty. |
| Does `StorageVolumeUsageHigh` fire from Forge? | **No.** It is only a playrule *name* if some **other** system sends that alertname. `monitoring/alerts.yml` does not define it. |

---

## Where you add it

1. UI **Assets** (`/assets`) → **Add**.
2. **Asset ID** — short slug, e.g. `flash-01`.
3. **Type** — **Storage** (group Storage; `QNAP/NAS` is the same SNMP family).
4. **IP** — Flash management IP (the one `snmpwalk` from this VM can reach).
5. **SNMP port** — leave empty for UDP/161, or set it (e.g. `1161`).
6. **SNMP version / community / v3** — match the array. Default community is `SNMP_COMMUNITY` from `secrets/secrets.env`. Custom or v3 needs `./forgesre snmp-auths` (also run by `update` / `render-monitoring`) before snmp_exporter uses `forgesre_<asset id>`.
7. Save. Scrape address stays **empty** (no `:9100` noise).

Discovery can also find a host that answers UDP/161. Approve it, then set Type to **Storage** if Classify guessed Network device.

Do **not** pick Linux Server. That path looks for `node_exporter` and will sit **Not scraped**.

---

## How it works inside Forge (real path)

```
Assets row (type Storage + IP)
        │
        ▼
GET /api/v1/sd/snmp     inventory.sd_snmp_targets()
        │  labels: asset, snmp_module=if_mib, snmp_auth, snmp_port
        ▼
Prometheus job forgesre-snmp     monitoring/prometheus.yml
        │  relabel target → 127.0.0.1:9116/snmp?module=if_mib&auth=…
        ▼
snmp_exporter     monitoring/snmp.yml  module if_mib only
        │  UDP/161 (or asset port) GET/WALK on the Flash IP
        ▼
Prometheus series     up, sysUpTime, ifOperStatus, ifHCInOctets, ifHCOutOctets
        │
        ├─ Asset page graphs     asset_metrics + rca.collector.promql_queries_for
        ├─ Interface list        dc_noc.snmp_surface
        ├─ Verify                asset_verify (up{job="forgesre-snmp",asset=…})
        └─ Alerts                SnmpDeviceUnreachable, NetworkInterfaceDown
```

Nothing in that chain reads an IBM enterprise OID.

---

## Exact code that owns each step

| Step | File | What it does |
|---|---|---|
| Type **Storage** is SNMP | `backend/app/asset_types.py` | `snmp_family()` — `storage` / `san` / `qnap` / `nas` match. Default UDP/161. |
| Target list | `backend/app/inventory.py` | `is_snmp_asset()`, `sd_snmp_targets()` — one HTTP SD row per Storage+IP. |
| Auth (community / v3) | `backend/app/asset_snmp.py` + `scripts/render_snmp_auths.py` | Writes `forgesre_<id>` into `data/generated/snmp.yml`. |
| Walk definition | `monitoring/snmp.yml` (+ `.tpl`) | **Only** `modules.if_mib`. Five metrics. No IBM module. |
| Prometheus scrape | `monitoring/prometheus.yml` | `job_name: forgesre-snmp` → `127.0.0.1:9116/snmp`. |
| Config knob | `backend/app/settings.py` | `snmp_module` defaults to `if_mib`. |
| Graphs / tiles | `agents/rca/collector.py` `promql_queries_for` | SNMP assets: `up`, `ifHCInOctets`, `ifHCOutOctets`, **`ifHighSpeed` (not collected)**. No CPU/disk. |
| Tile class | `backend/app/asset_metrics.py` | Storage → class `network` → **NET** tile only (`_SNMP_FAMILY`). |
| Interface table | `backend/app/dc_noc.py` `snmp_surface` | `ifOperStatus` + `sysUpTime` by `asset=`. |
| Verify | `backend/app/asset_verify.py` | Family “SNMP if_mib”; Prom `up{job="forgesre-snmp"}`. |
| Alerts | `monitoring/alerts.yml` group `forgesre-snmp` | Unreachable (`up==0` 2m), interface down (`ifOper==2` and `ifAdmin==1` 5m). |
| Playrule names | `agents/rca/catalog.py` | `snmp-down`, `if-down`. `StorageVolumeUsageHigh` is **name-only** — Forge never evaluates that PromQL. |
| UI copy | `frontend/templates/asset_detail.html` | “module if_mib” / “No extra vendor MIB was added.” |
| Tests that lock this | `tests/test_dc_noc_snmp.py`, `tests/test_ibm_flash_snmp.py` | Modules stay `{if_mib}`; Storage is SNMP; no IBM OID in the walk. |

Linux/Windows graphs (`node_*`, `windows_*`) **never** run for Type Storage. That skip is intentional (`test_linux_network_is_two_real_lines_and_snmp_skips_cpu`).

---

## What works vs what does not

### Works (not fake)

- Adding the array as a **Storage** asset.
- HTTP SD including it after save (if `snmp.enabled` and the row has an IP).
- snmp_exporter walking **IF-MIB** when UDP/161 (or the set port) and the community/v3 user are right.
- `up{job="forgesre-snmp",asset="<id>"}` = 1 after a good walk.
- Asset **Collecting** green; **NET** chart from `rate(ifHCInOctets)` / `ifHCOutOctets` if the controller exports those counters on its ethernet ports.
- Interface list (ifName / ifIndex / ifAlias / up|down) and sysUpTime on the asset page.
- Incidents **SnmpDeviceUnreachable** and **NetworkInterfaceDown** (mgmt/data **interfaces**, not vdisks).
- `./forgesre verify <id>` and `./forgesre snmp` against that real path.

Seeded demo network rows are **not** a live walk. A real agent (Flash SNMP, a switch, or `snmpd`) is required.

### Does not work (missing, not a hidden feature)

| Expectation | Reality |
|---|---|
| Pool / MDisk / VDisk used % | No OID walked. No PromQL. Charts stay **Not scraped.** |
| Enclosure, battery, node HA, IBM health | Not in `snmp.yml`. |
| IBM Flash “CPU” / controller % | Storage class has no CPU tile. |
| `ifHighSpeed` / link-speed line | Queried in `promql_queries_for` (`net_speed`) but **not** in the if_mib walk. Empty. |
| `StorageVolumeUsageHigh` from Forge | Not in `alerts.yml`. Playrule only attaches **if something else** (e.g. Zabbix) posts that alertname. |
| Vendor module from Type=Storage | `snmp_module` is always `if_mib` for every SNMP asset. |

This is the same honesty already in `docs/operator-handbook.md` and `docs/v0.5.md`: *no full vendor MIB pack*.

---

## How to see what is happening (on the VM)

Do these in order. If a step fails, stop — later steps will lie.

1. **Asset exists**
   - UI `/assets` → the row, Type Storage, IP set.
2. **Core publishes it**
   ```bash
   ./forgesre snmp
   ```
   Expect the asset id and `ip` / `ip:port` in the SNMP SD list. Empty list → type/IP/`snmp.enabled`.
3. **HTTP SD (same as Prometheus)**
   ```bash
   curl -sS -H "Authorization: Bearer $FORGESRE_WEBHOOK_TOKEN" \
     http://127.0.0.1:8080/api/v1/sd/snmp
   ```
   Look for `"asset":"flash-01"`, `"snmp_module":"if_mib"`.
4. **Exporter walk (bypasses Prometheus)**
   ```bash
   curl -sS 'http://127.0.0.1:9116/snmp?target=10.20.30.40&module=if_mib&auth=public_v2'
   ```
   Use `auth=forgesre_flash-01` after `./forgesre snmp-auths` if you set a Custom community or v3.  
   - HTTP 200 + `ifHCInOctets` / `ifOperStatus` → the array answered IF-MIB.  
   - Timeout / empty / error → UDP/161 ACL, wrong community, SNMP off on the Flash, or wrong IP. **Forge is not inventing samples.**
5. **Prometheus**
   ```text
   up{job="forgesre-snmp",asset="flash-01"}
   ifOperStatus{job="forgesre-snmp",asset="flash-01"}
   ifHCInOctets{job="forgesre-snmp",asset="flash-01"}
   ifHighSpeed{job="forgesre-snmp",asset="flash-01"}
   ```
   Last query **should be empty** today. That is the unused PromQL, not a broken Flash.
6. **Verify / doctor**
   ```bash
   ./forgesre verify flash-01
   ./forgesre doctor
   ```
   Verify says SNMP if_mib PASS when `up==1`. Doctor SNMP tile is **paused (no SNMP targets)** only when **no** Storage/Network+IP exists at all.
7. **UI**
   - Asset page: Collecting, NET chart, interface list, the if_mib sentence.
   - Soft-fail: Collecting yellow/red + **Not scraped.** on NET → walk failed or Prometheus has no series yet (~30s scrape).

From a workstation, tunnel if you must: `ssh -L 9090:127.0.0.1:9090 -L 9116:127.0.0.1:9116 <appliance>`.

---

## How to fix it (if you need real IBM Flash metrics)

Do **not** invent OIDs in `snmp.yml` by hand. snmp_exporter v0.26 rejects a bad module and takes **all** SNMP polling down.

1. **Keep today’s IF-MIB asset** so `up` + interfaces stay live while you add a module.
2. Get IBM’s FlashSystem / Storwize MIBs (the package IBM documents for that generation). Put them in a generator tree — not in git until legal/review is clear.
3. Run **prometheus/snmp_exporter generator** to emit a **second** module (e.g. `ibm_flash`) with only the tables you need (pool used, vdisk, enclosure state).
4. Merge that module next to `if_mib` in `monitoring/snmp.yml` + `.tpl`. Keep per-metric lookups (no module-level `lookups`).
5. Decide scrape: a second Prometheus job, or HTTP SD `snmp_module` per asset (`settings.snmp_module` is global today — that must change if Storage ≠ Switch).
6. Add PromQL + tiles in `agents/rca/collector.py` and `backend/app/asset_metrics.py` (capacity %, health). Do not reuse Linux `node_filesystem_*`.
7. Add `monitoring/alerts.yml` rules on **those** series. Do not pretend `StorageVolumeUsageHigh` / `filesystem_usage` is a SAN pool.
8. Lock it with tests next to `tests/test_ibm_flash_snmp.py` and `tests/test_dc_noc_snmp.py`.
9. Either **walk `ifHighSpeed`** in if_mib or **delete `net_speed`** from `promql_queries_for` so graphs stop asking for a missing series.

Until that ships, treat IBM Flash in Forge as **“mgmt IP is up, these ethernet ifs are up, octet rates”** — a network device that happens to be storage.

---

## Related

- [Operator handbook § network / SNMP](operator-handbook.md)
- [CLI `snmp` / `snmp-auths` / `verify`](cli.md)
- [V0.5 bundled snmp_exporter](v0.5.md)
- [Architecture](architecture.md)
