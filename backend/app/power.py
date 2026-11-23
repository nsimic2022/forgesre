"""Power and environment: UPS, PDU and sensor readings from the SNMP ``power`` module.

Prometheus scrapes UPS-MIB (RFC 1628) and ENTITY-SENSOR-MIB (RFC 3433) through job
``forgesre-snmp-power``. This module picks which assets join that job and builds the
"Power and environment" panel from a handful of instant queries (each with a timeout).
"""

from __future__ import annotations

from typing import Any, Callable

from sqlalchemy.orm import Session

from app.asset_types import POWER_TYPES, snmp_poll_on
from app.models import Asset

POWER_MODULE = "power"
POWER_JOB = "forgesre-snmp-power"
POWER_KEY = "snmp_power"

# ENTITY-SENSOR-MIB entPhySensorType values the panel shows.
SENSOR_KINDS = {3: "volts_ac", 4: "volts_dc", 5: "amperes", 6: "watts", 8: "celsius", 9: "humidity"}
SENSOR_UNITS = {"volts_ac": "V", "volts_dc": "V", "amperes": "A", "watts": "W", "celsius": "°C", "humidity": "%RH"}
# entPhySensorScale: 8 milli, 9 units, 10 kilo.
_SCALE = {8: 1e-3, 9: 1.0, 10: 1e3}
# upsOutputSource: 5 battery, 4 bypass.
OUTPUT_SOURCE = {1: "other", 2: "none", 3: "normal", 4: "bypass", 5: "battery", 6: "booster", 7: "reducer"}
BATTERY_STATUS = {1: "unknown", 2: "normal", 3: "low", 4: "depleted"}
# Same numbers as the PowerTemperatureHigh / UpsLoadHigh rules in monitoring/alerts.yml.
TEMP_WARN_C = 32.0
TEMP_CRIT_C = 40.0
UPS_LOAD_WARN = 80.0

_JOB = f'job="{POWER_JOB}"'
QUERIES = {
    "battery_status": f"upsBatteryStatus{{{_JOB}}}",
    "on_battery_s": f"upsSecondsOnBattery{{{_JOB}}}",
    "minutes_left": f"upsEstimatedMinutesRemaining{{{_JOB}}}",
    "charge": f"upsEstimatedChargeRemaining{{{_JOB}}}",
    "battery_temp": f"upsBatteryTemperature{{{_JOB}}}",
    "output_source": f"upsOutputSource{{{_JOB}}}",
    "alarms": f"upsAlarmsPresent{{{_JOB}}}",
    "load": f"max by (asset) (upsOutputPercentLoad{{{_JOB}}})",
    "input_v": f"min by (asset) (upsInputVoltage{{{_JOB}}})",
    "sensor_value": f"entPhySensorValue{{{_JOB}}}",
    "sensor_type": f"entPhySensorType{{{_JOB}}}",
    "sensor_scale": f"entPhySensorScale{{{_JOB}}}",
    "sensor_precision": f"entPhySensorPrecision{{{_JOB}}}",
    "sensor_status": f"entPhySensorOperStatus{{{_JOB}}}",
    "up": f"up{{{_JOB}}}",
}


def _extras(asset: Any) -> dict:
    raw = asset.get("extras") if isinstance(asset, dict) else getattr(asset, "extras", None)
    return raw if isinstance(raw, dict) else {}


def power_metrics_on(asset: Any) -> bool:
    """UPS, PDU and Environment sensor types always; any other type when extras ``snmp_power`` is on."""
    type_ = str((asset.get("type") if isinstance(asset, dict) else getattr(asset, "type", "")) or "").strip()
    if type_ in POWER_TYPES:
        return True
    return snmp_poll_on({"snmp_poll": _extras(asset).get(POWER_KEY)})


def sensor_reading(value: float, scale: float | int | None, precision: float | int | None) -> float:
    """Raw entPhySensorValue → real units (scale and decimal places from the same table row)."""
    factor = _SCALE.get(int(scale or 9), 1.0)
    places = int(precision or 0)
    return round(value * factor / (10**places if places > 0 else 1), 2)


def _by_asset(rows: list[dict]) -> dict[str, float]:
    out: dict[str, float] = {}
    for row in rows:
        asset = str((row.get("labels") or {}).get("asset") or "")
        if asset:
            out[asset] = row["value"]
    return out


def _by_sensor(rows: list[dict]) -> dict[tuple[str, str], tuple[float, str]]:
    out: dict[tuple[str, str], tuple[float, str]] = {}
    for row in rows:
        labels = row.get("labels") or {}
        asset, index = str(labels.get("asset") or ""), str(labels.get("entPhysicalIndex") or "")
        if asset and index:
            out[(asset, index)] = (row["value"], str(labels.get("entPhysicalName") or f"sensor {index}"))
    return out


def ups_state(row: dict[str, Any], *, temp_warn: float, load_warn: float) -> str:
    """ok | warning | critical for one UPS row; on battery or battery low/depleted is critical."""
    if row.get("source") == "battery" or row.get("battery") in {"low", "depleted"}:
        return "critical"
    if (row.get("load") or 0) >= load_warn or (row.get("battery_temp") or 0) >= temp_warn or (row.get("alarms") or 0) > 0:
        return "warning"
    if row.get("source") == "bypass":
        return "warning"
    return "ok"


def overview(db: Session, vector_fn: Callable[[str], dict] | None = None) -> dict[str, Any]:
    """Panel data: one row per UPS, one per sensor, plus the power assets that answer nothing.

    Prometheus errors are reported in ``error`` and the panel still lists the assets.
    """
    from app.settings import settings

    if vector_fn is None:
        from app.dc_noc import query_prometheus_vector as vector_fn  # 2 s timeout per query
    temp_warn, load_warn = TEMP_WARN_C, UPS_LOAD_WARN
    assets = [a for a in db.query(Asset).order_by(Asset.asset_id).all() if power_metrics_on(a)]
    names = {a.asset_id: a for a in assets}
    if not assets or not settings.snmp_enabled:
        return {"ups": [], "sensors": [], "silent": [a.asset_id for a in assets], "error": "", "temp_warn": temp_warn, "load_warn": load_warn}
    results: dict[str, list[dict]] = {}
    error = ""
    for key, expr in QUERIES.items():
        got = vector_fn(expr)
        if got.get("error"):
            error = str(got["error"])
            break
        results[key] = list(got.get("series") or [])
    if error:
        return {"ups": [], "sensors": [], "silent": [a.asset_id for a in assets], "error": error, "temp_warn": temp_warn, "load_warn": load_warn}

    cols = {key: _by_asset(rows) for key, rows in results.items() if not key.startswith("sensor_")}
    ups: list[dict[str, Any]] = []
    for asset_id in sorted(set(cols["battery_status"]) | set(cols["output_source"]) | set(cols["charge"])):
        row = {
            "asset": asset_id,
            "battery": BATTERY_STATUS.get(int(cols["battery_status"].get(asset_id, 1)), "unknown"),
            "source": OUTPUT_SOURCE.get(int(cols["output_source"].get(asset_id, 1)), "other"),
            "charge": cols["charge"].get(asset_id),
            "minutes_left": cols["minutes_left"].get(asset_id),
            "on_battery_s": cols["on_battery_s"].get(asset_id),
            "load": cols["load"].get(asset_id),
            "input_v": cols["input_v"].get(asset_id),
            "battery_temp": cols["battery_temp"].get(asset_id),
            "alarms": int(cols["alarms"].get(asset_id) or 0),
        }
        row["state"] = ups_state(row, temp_warn=temp_warn, load_warn=load_warn)
        ups.append(row)

    values = _by_sensor(results["sensor_value"])
    kinds = {k: v for k, (v, _n) in _by_sensor(results["sensor_type"]).items()}
    scales = {k: v for k, (v, _n) in _by_sensor(results["sensor_scale"]).items()}
    places = {k: v for k, (v, _n) in _by_sensor(results["sensor_precision"]).items()}
    status = {k: v for k, (v, _n) in _by_sensor(results["sensor_status"]).items()}
    sensors: list[dict[str, Any]] = []
    for key in sorted(values):
        kind = SENSOR_KINDS.get(int(kinds.get(key, 0)))
        if not kind:
            continue
        raw, name = values[key]
        value = sensor_reading(raw, scales.get(key), places.get(key))
        state = "ok"
        if int(status.get(key, 1)) != 1:
            state = "warning"
        elif kind == "celsius" and value >= temp_warn:
            state = "critical" if value >= TEMP_CRIT_C else "warning"
        sensors.append({"asset": key[0], "name": name, "kind": kind, "unit": SENSOR_UNITS[kind], "value": value, "state": state})

    answered = {r["asset"] for r in ups} | {s["asset"] for s in sensors}
    up = cols.get("up", {})
    silent = [a for a in names if a not in answered or up.get(a) == 0]
    return {"ups": ups, "sensors": sensors, "silent": silent, "error": "", "temp_warn": temp_warn, "load_warn": load_warn}
