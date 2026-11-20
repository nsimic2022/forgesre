"""Add many assets from a CSV file (Inventory › Import CSV in the new UI).

Two steps, so nothing is written by surprise:

1. ``preview`` parses the file and checks every row against the same rules as Add asset
   (hostname, IP without port, asset id, known type, field lengths, no duplicate id or IP in the
   file or in the inventory). It writes nothing.
2. ``run_import`` (job ``asset_import``) creates the rows the preview accepted, one by one, through
   ``create_manual_asset``, then sets parents. A row that fails is reported and the rest go on.

Existing assets are never changed: a row whose id or IP is already in the inventory is skipped.
SNMP communities and v3 passwords are not read from CSV; set them on the asset afterwards.
"""

from __future__ import annotations

import csv
import io
from typing import Any

from sqlalchemy.orm import Session

from app.asset_extras import EXTRA_FIELDS
from app.asset_types import ASSET_TYPE_CHOICES
from app.models import Asset

IMPORT_KIND = "asset_import"
MAX_BYTES = 1_000_000
MAX_ROWS = 2000

BASE_COLUMNS = (
    "hostname", "ip", "type", "environment", "owner", "contact_name", "owner_email", "owner_phone",
    "notes", "monitoring_profile", "scrape_address", "asset_id", "parent",
)
EXTRA_LIMITS = {key: limit for key, _label, limit in EXTRA_FIELDS}
COLUMNS = BASE_COLUMNS + tuple(EXTRA_LIMITS)
ALIASES = {
    "host": "hostname", "name": "hostname", "host_name": "hostname",
    "address": "ip", "ip_address": "ip", "ipaddress": "ip",
    "asset_type": "type", "kind": "type",
    "env": "environment",
    "contact": "contact_name", "email": "owner_email", "phone": "owner_phone",
    "profile": "monitoring_profile", "scrape": "scrape_address",
    "id": "asset_id", "uplink": "parent", "parent_asset": "parent",
    "domain": "customer", "dc": "site", "room": "site",
}
TYPES = {name.lower(): name for name in ASSET_TYPE_CHOICES}
TEXT_LIMIT = 255
SAMPLE = "hostname,ip,type,environment,owner,site,rack,parent\nweb-01,10.0.0.11,Linux Server,Production,platform,DC1,A07,sw-core-01\n"


def _column(name: str) -> str:
    key = "_".join((name or "").strip().lower().replace("-", " ").replace("/", " ").split())
    return ALIASES.get(key, key)


def _read(text: str) -> tuple[list[str], list[dict[str, str]]]:
    """Header and rows. Comma, semicolon (Excel in many locales) or tab; a UTF-8 BOM is dropped."""
    body = (text or "").lstrip("﻿")
    if not body.strip():
        raise ValueError("The file is empty.")
    if len(body.encode("utf-8")) > MAX_BYTES:
        raise ValueError("The file is larger than 1 MB; split it.")
    first = body.splitlines()[0]
    delimiter = max((",", ";", "\t"), key=first.count)
    reader = csv.reader(io.StringIO(body), delimiter=delimiter)
    header = [_column(cell) for cell in next(reader, [])]
    if "hostname" not in header:
        raise ValueError("The first line must name the columns and include hostname.")
    rows: list[dict[str, str]] = []
    for cells in reader:
        if not any((cell or "").strip() for cell in cells):
            continue
        rows.append({key: (cells[i] if i < len(cells) else "").strip() for i, key in enumerate(header) if key in COLUMNS})
        if len(rows) > MAX_ROWS:
            raise ValueError(f"More than {MAX_ROWS} rows; split the file.")
    return header, rows


def _check(row: dict[str, str]) -> tuple[dict[str, Any], str]:
    """One row as create_manual_asset arguments, or an error in plain words."""
    from app.inventory import asset_id_slug, validate_asset_id, validate_ip_field

    hostname = row.get("hostname", "")
    if not hostname:
        return {}, "hostname is empty"
    try:
        ip = validate_ip_field(row.get("ip", ""))
        slug = validate_asset_id(row.get("asset_id") or asset_id_slug(hostname, ip))
    except ValueError as exc:
        return {}, str(exc)
    raw_type = row.get("type", "")
    kind = TYPES.get(raw_type.lower()) if raw_type else "Linux Server"
    if kind is None:
        return {}, f"unknown type {raw_type!r}; use one of: {', '.join(ASSET_TYPE_CHOICES)}"
    for key in BASE_COLUMNS:
        if len(row.get(key, "")) > (4000 if key == "notes" else TEXT_LIMIT):
            return {}, f"{key} is too long"
    extras = {}
    for key, limit in EXTRA_LIMITS.items():
        value = row.get(key, "")
        if len(value) > limit:
            return {}, f"{key} is longer than {limit} characters"
        if value:
            extras[key] = value
    args = {
        "hostname": hostname, "ip": ip, "type": kind, "asset_id": slug,
        "environment": row.get("environment") or "Production", "owner": row.get("owner") or "platform",
        "contact_name": row.get("contact_name", ""), "owner_email": row.get("owner_email", ""),
        "owner_phone": row.get("owner_phone", ""), "notes": row.get("notes", ""),
        "monitoring_profile": row.get("monitoring_profile", ""), "scrape_address": row.get("scrape_address", ""),
        "extras": extras, "parent": row.get("parent", ""),
    }
    return args, ""


def preview(db: Session, text: str) -> dict[str, Any]:
    """Check a CSV without writing. Each row gets action create, exists (skipped) or error.

    Raises ValueError for a file that cannot be read at all (empty, too big, no hostname column).
    """
    header, rows = _read(text)
    known = {a.asset_id: a for a in db.query(Asset).all()}
    by_ip = {a.ip: a.asset_id for a in known.values() if a.ip}
    by_name = {a.hostname.lower(): a.asset_id for a in known.values() if a.hostname}
    seen_id: dict[str, int] = {}
    seen_ip: dict[str, int] = {}
    out: list[dict[str, Any]] = []
    for index, raw in enumerate(rows, start=2):
        args, error = _check(raw)
        entry: dict[str, Any] = {"line": index, "hostname": raw.get("hostname", ""), "ip": raw.get("ip", ""),
                                 "type": args.get("type", raw.get("type", "")), "asset_id": args.get("asset_id", "")}
        if not error and args["asset_id"] in seen_id:
            error = f"same asset id as line {seen_id[args['asset_id']]}"
        if not error and args["ip"] and args["ip"] in seen_ip:
            error = f"same IP as line {seen_ip[args['ip']]}"
        if error:
            out.append({**entry, "action": "error", "reason": error})
            continue
        seen_id[args["asset_id"]] = index
        if args["ip"]:
            seen_ip[args["ip"]] = index
        if args["asset_id"] in known or (args["ip"] and args["ip"] in by_ip):
            other = args["asset_id"] if args["asset_id"] in known else by_ip[args["ip"]]
            out.append({**entry, "action": "exists", "reason": f"already in the inventory as {other}; not changed"})
            continue
        parent = args["parent"]
        if parent:
            resolved = parent if parent in known else by_name.get(parent.lower(), "")
            in_file = next((r["args"]["asset_id"] for r in out if r.get("args") and parent in (r["args"]["asset_id"], r["args"]["hostname"])), "")
            if not (resolved or in_file):
                out.append({**entry, "action": "error", "reason": f"parent {parent!r} is not in the inventory or earlier in the file"})
                continue
            args["parent"] = resolved or in_file
        out.append({**entry, "action": "create", "reason": "", "args": args})
    counts = {name: sum(1 for r in out if r["action"] == name) for name in ("create", "exists", "error")}
    ignored = sorted({h for h in header if h and h not in COLUMNS})
    return {"rows": out, "counts": counts, "ignored_columns": ignored, "columns": list(COLUMNS)}


def public(result: dict[str, Any]) -> dict[str, Any]:
    """Preview for the browser: the internal create arguments are left out."""
    return {**result, "rows": [{k: v for k, v in r.items() if k != "args"} for r in result["rows"]]}


def run_import(db: Session, rows: list[dict[str, Any]], actor: str) -> dict[str, Any]:
    """Job body: create each previewed row, then link parents. Probes (Auto detect) have their own timeouts."""
    from app.dc_noc import set_asset_parent
    from app.inventory import create_manual_asset
    from app.journal import report

    created: list[str] = []
    failed: list[dict[str, Any]] = []
    parents: list[tuple[str, str, int]] = []
    for row in rows:
        args = dict(row.get("args") or {})
        parent = args.pop("parent", "")
        try:
            asset = create_manual_asset(db, actor=actor, require_new=True, **args)
        except ValueError as exc:
            db.rollback()
            failed.append({"line": row.get("line"), "hostname": args.get("hostname", ""), "reason": str(exc)})
            continue
        created.append(asset.asset_id)
        if parent:
            parents.append((asset.asset_id, parent, int(row.get("line") or 0)))
    for asset_id, parent, line in parents:
        asset = db.query(Asset).filter_by(asset_id=asset_id).first()
        try:
            set_asset_parent(db, asset, parent)
            db.commit()
        except ValueError as exc:
            db.rollback()
            failed.append({"line": line, "hostname": asset_id, "reason": f"added, but parent not set: {exc}"})
    report(db, "inventory", "asset.import", "warn" if failed else "ok",
           summary=f"CSV import: {len(created)} added, {len(failed)} with problems", detail=f"actor={actor}")
    return {"created": created, "failed": failed}
