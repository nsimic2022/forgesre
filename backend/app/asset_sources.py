"""One inventory, many feeders: matching and field ownership for GLPI, Zabbix, NetBox and LibreNMS.

Every sync turns each outside record into a ``SourceRecord`` and calls ``match`` and ``reconcile``:

- **Matching**, strongest key first: the source's own id (link), then serial, MAC, name
  (hostname, asset id or sysName) and IP. When two keys point at two different assets, the
  strongest wins and a ``match`` conflict is opened for review. Nothing is ever duplicated.
- **Field ownership.** An empty field is filled. A field the source owns (``OWNED``) follows the
  source while nobody else changed it since the source wrote it. Any other difference (a person's
  edit, another source's value, a different IP) becomes an open ``AssetConflict``; it is never
  written silently. An Engineer keeps ours or takes theirs; a decision is remembered while the
  source keeps sending the same value.
- **Identity.** What each source said is kept in ``AssetIdentity`` (serial, MAC, name, IP, the
  values it wrote), so the next sync matches by serial or MAC even when the name or IP changed.

Owner, contact, email, phone, notes and type are never written by a source.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy.orm import Session

from app.models import Asset, AssetConflict, AssetIdentity, utcnow

SOURCES = ("glpi", "zabbix", "netbox", "librenms")
LINK_COLUMNS = {"glpi": "glpi_id", "zabbix": "zabbix_hostid", "netbox": "netbox_id"}
# Fields a source owns: it may update them later, as long as the value is still the one it wrote.
OWNED = {"glpi": {"site", "customer", "contract"}, "netbox": {"site", "customer"}, "zabbix": set(), "librenms": set()}
EXTRA_FIELDS = {"site", "customer", "contract", "row", "rack"}
LABELS = {"ip": "IP", "site": "Site", "customer": "Customer", "contract": "Contract", "row": "Row", "rack": "Rack"}


@dataclass
class SourceRecord:
    source: str
    source_id: str = ""
    name: str = ""
    ip: str = ""
    serial: str = ""
    mac: str = ""
    sys_name: str = ""
    fields: dict[str, str] = field(default_factory=dict)

    def label(self) -> str:
        return f"{self.source} {self.name or self.source_id or self.ip}".strip()


def norm_mac(value: str) -> str:
    hexes = re.sub(r"[^0-9a-f]", "", (value or "").lower())
    return ":".join(hexes[i:i + 2] for i in range(0, 12, 2)) if len(hexes) == 12 else ""


def norm_serial(value: str) -> str:
    text = re.sub(r"\s+", "", (value or "").strip().upper())
    return "" if text in {"", "NONE", "N/A", "NA", "0", "DEFAULTSTRING", "TOBEFILLEDBYO.E.M."} else text


def _loopback(ip: str) -> bool:
    return ip.startswith("127.") or ip in {"::1", "0.0.0.0", ""}


@dataclass
class Index:
    link: dict[tuple[str, str], Asset] = field(default_factory=dict)
    serial: dict[str, Asset] = field(default_factory=dict)
    mac: dict[str, Asset] = field(default_factory=dict)
    name: dict[str, Asset] = field(default_factory=dict)
    ip: dict[str, Asset] = field(default_factory=dict)

    def add(self, asset: Asset, rec: SourceRecord | None = None) -> None:
        for key in (asset.hostname, asset.asset_id):
            if (key or "").strip():
                self.name.setdefault(key.strip().lower(), asset)
        if (asset.ip or "").strip() and not _loopback(asset.ip.strip()):
            self.ip.setdefault(asset.ip.strip(), asset)
        for source, column in LINK_COLUMNS.items():
            value = str(getattr(asset, column, "") or "").strip()
            if value:
                self.link.setdefault((source, value), asset)
        if rec is not None:
            if rec.source_id:
                self.link[(rec.source, rec.source_id)] = asset
            if norm_serial(rec.serial):
                self.serial.setdefault(norm_serial(rec.serial), asset)
            if norm_mac(rec.mac):
                self.mac.setdefault(norm_mac(rec.mac), asset)
            for key in (rec.name, rec.sys_name):
                if (key or "").strip():
                    self.name.setdefault(key.strip().lower(), asset)


def build_index(db: Session) -> Index:
    """Lookup tables for one sync. DEMO lab rows are left out so a source never links to them."""
    from app.demo_ids import is_lab_inventory_row

    index = Index()
    assets = {a.id: a for a in db.query(Asset).order_by(Asset.id).all() if not is_lab_inventory_row(a)}
    for asset in assets.values():
        index.add(asset)
    for row in db.query(AssetIdentity).order_by(AssetIdentity.id).all():
        asset = assets.get(row.asset_id)
        if asset is not None:
            index.add(asset, SourceRecord(source=row.source, source_id=row.source_id, name=row.name,
                                          serial=row.serial, mac=row.mac))
    return index


def match(index: Index, rec: SourceRecord) -> tuple[Asset | None, str]:
    """(asset, disagreement). The strongest key wins; a different asset behind a weaker key is reported."""
    hits: list[tuple[str, Asset]] = []
    if rec.source_id and (rec.source, rec.source_id) in index.link:
        hits.append((f"{rec.source} id", index.link[(rec.source, rec.source_id)]))
    if norm_serial(rec.serial) in index.serial:
        hits.append(("serial", index.serial[norm_serial(rec.serial)]))
    if norm_mac(rec.mac) in index.mac:
        hits.append(("MAC", index.mac[norm_mac(rec.mac)]))
    for key in (rec.name, rec.sys_name):
        if (key or "").strip().lower() in index.name:
            hits.append(("name", index.name[key.strip().lower()]))
            break
    if rec.ip and not _loopback(rec.ip) and rec.ip in index.ip:
        hits.append(("IP", index.ip[rec.ip]))
    if not hits:
        return None, ""
    how, chosen = hits[0]
    others = [(k, a) for k, a in hits[1:] if a.id != chosen.id]
    if not others:
        return chosen, ""
    k, other = others[0]
    return chosen, f"matched {chosen.asset_id} by {how}, but its {k} belongs to {other.asset_id}"


def _current(asset: Asset, name: str) -> str:
    if name == "ip":
        return str(asset.ip or "").strip()
    extras = asset.extras if isinstance(asset.extras, dict) else {}
    return str(extras.get(name) or "").strip()


def _write(asset: Asset, name: str, value: str) -> None:
    if name == "ip":
        asset.ip = value
        return
    extras = dict(asset.extras or {}) if isinstance(asset.extras, dict) else {}
    extras[name] = value
    asset.extras = extras


def open_conflict(db: Session, asset: Asset | None, rec: SourceRecord, kind: str, name: str = "",
                  ours: str = "", theirs: str = "", detail: str = "") -> AssetConflict | None:
    """One open conflict per asset, source and field. A decided one stays decided while the source repeats itself."""
    query = db.query(AssetConflict).filter(AssetConflict.source == rec.source, AssetConflict.kind == kind,
                                           AssetConflict.field == name)
    query = query.filter(AssetConflict.asset_id == (asset.id if asset is not None else None)) if asset is not None \
        else query.filter(AssetConflict.asset_id.is_(None), AssetConflict.source_id == rec.source_id)
    row = query.order_by(AssetConflict.id.desc()).first()
    if row is not None and (row.status == "open" or row.theirs == theirs):
        row.seen_at = utcnow()
        if row.status == "open":
            row.ours, row.theirs, row.detail = ours, theirs, detail or row.detail
        return row if row.status == "open" else None
    row = AssetConflict(asset_id=asset.id if asset is not None else None, source=rec.source, source_id=rec.source_id,
                        kind=kind, field=name, ours=ours, theirs=theirs, detail=detail)
    db.add(row)
    db.flush()
    return row


def remember(db: Session, asset: Asset, rec: SourceRecord) -> AssetIdentity:
    row = None
    if rec.source_id:
        row = db.query(AssetIdentity).filter_by(source=rec.source, source_id=rec.source_id).first()
    if row is None:
        row = db.query(AssetIdentity).filter_by(source=rec.source, asset_id=asset.id).first()
    if row is None:
        row = AssetIdentity(asset_id=asset.id, source=rec.source, source_id=rec.source_id, fields={})
        db.add(row)
        db.flush()  # sessions run without autoflush; the next record of this sync must find it
    row.asset_id = asset.id
    row.source_id = rec.source_id or row.source_id
    row.name = (rec.name or rec.sys_name or row.name or "")[:255]
    row.ip = (rec.ip or row.ip or "")[:64]
    row.serial = (norm_serial(rec.serial) or row.serial or "")[:128]
    row.mac = (norm_mac(rec.mac) or row.mac or "")[:64]
    row.seen_at = utcnow()
    return row


def reconcile(db: Session, index: Index, asset: Asset, rec: SourceRecord, *, disagreement: str = "") -> dict[str, int]:
    """Link, remember and apply one record to its asset. Returns {"filled", "updated", "conflicts"}."""
    out = {"filled": 0, "updated": 0, "conflicts": 0}
    column = LINK_COLUMNS.get(rec.source)
    if column and rec.source_id and not str(getattr(asset, column, "") or "").strip():
        setattr(asset, column, rec.source_id)
    if disagreement and open_conflict(db, asset, rec, "match", detail=disagreement):
        out["conflicts"] += 1
    identity = remember(db, asset, rec)
    wrote = dict(identity.fields or {})
    wanted = {"ip": rec.ip} if rec.ip and not _loopback(rec.ip) else {}
    wanted.update({k: v for k, v in rec.fields.items() if k in EXTRA_FIELDS and str(v or "").strip()})
    for name, value in wanted.items():
        value = str(value).strip()
        current = _current(asset, name)
        if current == value:
            wrote[name] = value
            continue
        if not current:
            _write(asset, name, value[:255])
            wrote[name] = value
            out["filled"] += 1
            continue
        if name in OWNED.get(rec.source, set()) and wrote.get(name) == current:
            _write(asset, name, value[:255])
            wrote[name] = value
            out["updated"] += 1
            continue
        if name == "ip":
            reason = "A sync never changes an IP."
        elif name in OWNED.get(rec.source, set()):
            reason = f"Changed in ForgeSRE after {rec.source} wrote it."
        else:
            reason = f"{rec.source} does not own this field."
        why = f"{LABELS.get(name, name)} in ForgeSRE is {current!r}; {rec.source} says {value!r}. {reason}"
        if open_conflict(db, asset, rec, "ip" if name == "ip" else "field", name, current, value, why):
            out["conflicts"] += 1
    identity.fields = wrote
    index.add(asset, rec)
    return out


def resolve(db: Session, conflict: AssetConflict, action: str, actor: str) -> AssetConflict:
    """Keep ours or take theirs. Taking theirs writes the value and records that the source wrote it."""
    from app.audit import audit

    if action not in {"ours", "theirs"}:
        raise ValueError("action is ours or theirs")
    if conflict.status != "open":
        raise ValueError("this conflict is already decided")
    asset = db.get(Asset, conflict.asset_id) if conflict.asset_id else None
    if action == "theirs":
        if conflict.kind not in {"field", "ip"} or asset is None:
            raise ValueError("only a field difference can be taken from the source; edit the asset for the rest")
        if conflict.field == "ip":
            from app.inventory import validate_ip_field

            validate_ip_field(conflict.theirs)
        _write(asset, conflict.field, conflict.theirs)
        identity = db.query(AssetIdentity).filter_by(source=conflict.source, asset_id=asset.id).first()
        if identity is not None:
            identity.fields = {**(identity.fields or {}), conflict.field: conflict.theirs}
    conflict.status, conflict.decided_by, conflict.decided_at = action, actor, utcnow()
    audit(db, "asset.conflict", actor=actor, object_type="asset", object_id=asset.asset_id if asset else conflict.source_id,
          data={"source": conflict.source, "field": conflict.field, "kept": action, "ours": conflict.ours, "theirs": conflict.theirs})
    db.commit()
    return conflict


def conflict_rows(db: Session, *, status: str = "open", limit: int = 200) -> list[dict[str, Any]]:
    rows = (db.query(AssetConflict).filter(AssetConflict.status == status)
            .order_by(AssetConflict.seen_at.desc()).limit(limit).all())
    assets = {a.id: a for a in db.query(Asset).filter(Asset.id.in_([r.asset_id for r in rows if r.asset_id])).all()}
    return [{
        "id": r.id, "source": r.source, "source_id": r.source_id, "kind": r.kind, "field": LABELS.get(r.field, r.field),
        "ours": r.ours, "theirs": r.theirs, "detail": r.detail, "status": r.status,
        "asset_id": assets[r.asset_id].asset_id if r.asset_id in assets else "",
        "hostname": assets[r.asset_id].hostname if r.asset_id in assets else "",
        "seen_at": r.seen_at.isoformat() if r.seen_at else None,
    } for r in rows]


def sources_view(db: Session) -> list[dict[str, Any]]:
    """Per source: on or off, last sync (time, status, summary), linked assets and open conflicts."""
    from sqlalchemy import func

    from app.models import JournalEntry
    from app.settings import settings

    enabled = {"glpi": settings.glpi_enabled, "zabbix": settings.zabbix_enabled, "netbox": settings.netbox_enabled,
               "librenms": bool(getattr(settings, "librenms_enabled", False) or getattr(settings, "librenms_url", ""))}
    journal = {"glpi": ("glpi", "sync"), "zabbix": ("zabbix", "sync"), "netbox": ("netbox", "sync"),
               "librenms": ("inventory", "topology.sync")}
    linked = dict(db.query(AssetIdentity.source, func.count(AssetIdentity.id)).group_by(AssetIdentity.source).all())
    open_ = dict(db.query(AssetConflict.source, func.count(AssetConflict.id))
                 .filter(AssetConflict.status == "open").group_by(AssetConflict.source).all())
    out = []
    for source in SOURCES:
        module, action = journal[source]
        last = (db.query(JournalEntry).filter_by(module=module, action=action).order_by(JournalEntry.id.desc()).first())
        out.append({
            "source": source, "enabled": bool(enabled[source]), "linked": int(linked.get(source, 0)),
            "conflicts": int(open_.get(source, 0)),
            "last": None if last is None else {"at": last.at.isoformat() if last.at else None,
                                               "status": last.status, "summary": last.summary},
        })
    return out


def new_device_mode() -> str:
    """'add' puts new devices from a source straight into the inventory; 'review' lists them in Discovery."""
    from app.settings import settings

    return "review" if str(getattr(settings, "inventory_new_from_sources", "add")) == "review" else "add"


def hold_for_review(db: Session, rec: SourceRecord, role: str) -> bool:
    """Review mode: list a new device in Discovery (source = the feeder) instead of adding it. False without an IP."""
    from app.inventory import upsert_candidate
    from app.models import DiscoveryCandidate

    if not rec.ip or _loopback(rec.ip):
        return False
    existing = db.query(DiscoveryCandidate).filter_by(ip=rec.ip).first()
    if existing is not None and existing.status == "ignored":
        return True  # an Engineer already said no; a sync does not bring it back
    row = upsert_candidate(db, rec.ip, role, [], source=rec.source)
    row.hostname = (rec.name or rec.sys_name or row.hostname or "")[:255]
    return True
