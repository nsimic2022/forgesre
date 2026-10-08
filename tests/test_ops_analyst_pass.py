"""Ops/analyst pass: re-fire after RESOLVED, dashboard = list, playrules, NetBox, escalation recipients."""

from __future__ import annotations

import re
from pathlib import Path
from uuid import uuid4

from fastapi.testclient import TestClient

from app.db import Base, SessionLocal, engine
from app.exporter_detect import AUTO_ASSET_TYPE
from app.inventory import (
    approve_candidate,
    asset_missing_email,
    create_manual_asset,
    delete_candidate,
    netbox_asset_id,
    seed_demo_candidate,
    sync_netbox,
)
from app.main import app
from app.models import Asset, AuditLog, DiscoveryCandidate, EscalationPolicy, Incident, Notification, Playrule
from app.seed import seed
from app.services import (
    NO_RECIPIENT_STATUS,
    ensure_notification,
    escalation_recipient,
    ingest_alertmanager,
    match_playrule,
)

ROOT = Path(__file__).resolve().parents[1]
INC_ID = re.compile(r"^INC-\d{4}_\d{2}\.\d{2}\.\d{4}_\d{2}:\d{2}$")


def _db():
    Base.metadata.create_all(bind=engine)
    db = SessionLocal()
    seed(db)
    return db


def _login(client: TestClient) -> None:
    client.post("/login", data={"email": "admin@forgesre.local", "password": "testpass"}, follow_redirects=False)


def _alert(alertname: str, asset: str, status: str = "firing", **labels) -> dict:
    merged = {"alertname": alertname, "severity": "warning", **labels}
    if asset:
        merged["asset"] = asset
    return {
        "status": status,
        "alerts": [{"status": status, "labels": merged, "annotations": {"summary": f"{alertname} on {asset}"}}],
    }


def _host(db, name: str, **extra) -> Asset:
    return create_manual_asset(
        db,
        hostname=name,
        ip=extra.pop("ip", "10.77.%d.%d" % (uuid4().int % 250 + 1, uuid4().int % 230 + 20)),
        type=extra.pop("type", "Linux Server"),
        actor="tester",
        **extra,
    )


def test_refire_after_resolved_opens_new_incident_and_links_both():
    db = _db()
    host = _host(db, f"refire-{uuid4().hex[:6]}", owner_email="refire@dc.local")
    first = ingest_alertmanager(db, _alert("HighCPU", host.asset_id))
    assert len(first) == 1
    original = first[0]
    assert INC_ID.match(original.number)
    assert ingest_alertmanager(db, _alert("HighCPU", host.asset_id, status="resolved")) == []
    db.refresh(original)
    assert original.status == "RESOLVED"

    again = ingest_alertmanager(db, _alert("HighCPU", host.asset_id))
    assert len(again) == 1
    fresh = again[0]
    assert fresh.number != original.number
    assert INC_ID.match(fresh.number)
    assert fresh.status in {"OPEN", "INVESTIGATING"}
    db.refresh(original)
    assert original.status == "RESOLVED"
    assert any(item["id"] == "refire" and fresh.number in item["detail"] for item in original.timeline)
    assert any(item["id"] == "refire" and original.number in item["detail"] for item in fresh.timeline)
    note = db.query(Notification).filter_by(incident_id=fresh.id, step_key="immediate").one()
    assert note.target == "refire@dc.local"

    assert ingest_alertmanager(db, _alert("HighCPU", host.asset_id)) == []
    db.close()


def test_firing_after_closed_opens_new_incident_and_closed_stays_closed():
    db = _db()
    host = _host(db, f"closed-{uuid4().hex[:6]}")
    created = ingest_alertmanager(db, _alert("NodeMemoryHigh", host.asset_id))
    row = created[0]
    row.status = "CLOSED"
    db.commit()
    again = ingest_alertmanager(db, _alert("NodeMemoryHigh", host.asset_id))
    assert len(again) == 1 and again[0].number != row.number
    db.refresh(row)
    assert row.status == "CLOSED"
    assert not any(item["id"] == "refire" for item in row.timeline)
    db.close()


def test_unlabeled_alert_does_not_attach_to_demo_host():
    db = _db()
    name = f"Unlabeled{uuid4().hex[:6]}"
    created = ingest_alertmanager(db, _alert(name, ""))
    assert len(created) == 1
    row = created[0]
    assert row.fingerprint == f"{name}:unlabeled"
    assert row.asset_id is None
    from app.services import is_demo_incident

    assert not is_demo_incident(row)
    db.close()


def test_match_playrule_is_alertname_only():
    db = _db()
    assert match_playrule(db, "HighCPU", {}).name == "high-cpu"
    assert match_playrule(db, "highcpu").name == "high-cpu"
    assert match_playrule(db, "cpu_usage", {"metric": "cpu_usage"}) is None
    assert match_playrule(db, "") is None
    db.close()


def _tile_counts(html: str) -> dict[str, tuple[str, int]]:
    out = {}
    for key, href, count in re.findall(
        r'<a class="stat[^"]*" href="([^"]+)" data-tile="([^"]+)"><span>[^<]*</span><strong>(\d+)</strong>',
        html,
    ):
        out[href] = (key, int(count))
    return {key: (href, count) for key, (href, count) in out.items()}


def test_dashboard_incident_tiles_match_the_list_behind_the_click():
    db = _db()
    host = _host(db, f"tiles-{uuid4().hex[:6]}")
    token = uuid4().hex[:6]
    for status, severity in [
        ("OPEN", "CRITICAL"),
        ("INVESTIGATING", "WARNING"),
        ("ESCALATED", "CRITICAL"),
        ("RESOLVED", "CRITICAL"),
        ("CLOSED", "WARNING"),
    ]:
        from app.services import next_incident_number

        db.add(
            Incident(
                number=next_incident_number(db),
                title=f"tile {status} {token}",
                severity=severity,
                status=status,
                fingerprint=f"tile:{token}:{status}",
                asset_id=host.id,
            )
        )
        db.flush()
    db.commit()
    client = TestClient(app)
    _login(client)
    home = client.get("/")
    assert home.status_code == 200
    tiles = _tile_counts(home.text)
    assert set(tiles) == {"open", "critical", "investigating", "escalated", "resolved"}
    assert tiles["critical"][0] == "/incidents?status=active&amp;severity=critical"
    assert tiles["open"][0] == "/incidents?status=unacked"
    assert tiles["escalated"][0] == "/incidents?status=ESCALATED"
    for key, (href, count) in tiles.items():
        listed = client.get(href.replace("&amp;", "&"))
        assert listed.status_code == 200
        shown = re.search(r'class="muted list-count">(\d+) incident', listed.text)
        assert shown is not None, key
        assert int(shown.group(1)) == count, key
    assert tiles["escalated"][1] >= 1
    critical = client.get("/incidents?status=active&severity=critical")
    body = critical.text.split("<tbody>", 1)[1].split("</tbody>", 1)[0]
    assert f"tile OPEN {token}" in body
    assert f"tile ESCALATED {token}" in body
    assert f"tile RESOLVED {token}" not in body
    assert f"tile INVESTIGATING {token}" not in body
    default = client.get("/incidents")
    assert f"tile CLOSED {token}" in default.text
    db.close()


def test_dashboard_asset_tiles_match_assets_filters():
    db = _db()
    host = _host(db, f"noemail-{uuid4().hex[:6]}")
    assert asset_missing_email(host)
    host.ping_status = "red"
    host.exporter_status = "red"
    db.commit()
    client = TestClient(app)
    _login(client)
    home = client.get("/")
    assert 'href="/assets?flag=no-email"' in home.text
    assert 'href="/assets?flag=unreachable"' in home.text
    listed = client.get(f"/assets?flag=no-email&q={host.asset_id}")
    assert listed.status_code == 200
    assert host.asset_id in listed.text
    assert "No owner email" in listed.text
    down = client.get(f"/assets?flag=unreachable&q={host.asset_id}")
    assert host.asset_id in down.text
    db.close()


def test_playrules_edit_delete_and_promql_preview():
    db = _db()
    client = TestClient(app)
    _login(client)
    page = client.get("/playrules")
    assert page.status_code == 200
    assert "forgesre_demo_cpu_percent &gt; 80" in page.text
    assert "not executed" in page.text
    assert "cannot fire earlier than the Prometheus rule" in page.text
    name = f"pr-{uuid4().hex[:6]}"
    created = client.post(
        "/playrules",
        data={"name": name, "alertname": "NoSuchAlert", "metric": "", "severity": "warning", "playbook_id": "", "escalation_policy_id": ""},
        follow_redirects=False,
    )
    assert created.status_code == 302
    db.expire_all()
    row = db.query(Playrule).filter_by(name=name).one()
    assert row.condition == {"alertname": "NoSuchAlert"}
    assert row.escalation_policy_id is not None
    listed = client.get("/playrules")
    assert "No Prometheus rule" in listed.text
    edit = client.get(f"/playrules?edit={row.id}")
    assert f'action="/playrules/{row.id}/update"' in edit.text
    updated = client.post(
        f"/playrules/{row.id}/update",
        data={"name": name, "alertname": "NodeCPUHigh", "metric": "cpu_usage", "operator": ">", "value": "85", "severity": "critical"},
        follow_redirects=False,
    )
    assert updated.status_code == 302
    db.expire_all()
    row = db.get(Playrule, row.id)
    assert row.condition["alertname"] == "NodeCPUHigh"
    assert row.condition["metric"] == "cpu_usage"
    assert row.severity == "critical"
    removed = client.post(f"/playrules/{row.id}/delete", follow_redirects=False)
    assert removed.status_code == 302
    db.expire_all()
    assert db.query(Playrule).filter_by(name=name).first() is None
    db.close()


def test_playrule_delete_keeps_incidents():
    db = _db()
    name = f"del-{uuid4().hex[:6]}"
    alertname = f"DelAlert{uuid4().hex[:6]}"
    db.add(Playrule(name=name, enabled=True, severity="warning", condition={"alertname": alertname}))
    db.commit()
    host = _host(db, f"del-{uuid4().hex[:6]}")
    incident = ingest_alertmanager(db, _alert(alertname, host.asset_id))[0]
    assert incident.playrule_id is not None
    client = TestClient(app)
    _login(client)
    rule = db.query(Playrule).filter_by(name=name).one()
    assert client.post(f"/playrules/{rule.id}/delete", follow_redirects=False).status_code == 302
    db.expire_all()
    kept = db.query(Incident).filter_by(number=incident.number).one()
    assert kept.playrule_id is None
    db.close()


def test_netbox_sync_does_not_guess_linux_or_overwrite_contacts(monkeypatch):
    db = _db()
    token = uuid4().hex[:6]
    existing = _host(
        db,
        f"nb-keep-{token}",
        owner="payments",
        contact_name="Milan",
        owner_email="milan@dc.local",
        notes="Operator note",
    )
    devices = [
        {
            "netbox_id": f"9{token}",
            "name": f"Core SW {token.upper()}",
            "ip": "10.88.0.5",
            "type": "C9300",
            "status": "active",
            "site": "DC1",
            "tenant": "Infra",
            "tags": ["core"],
        },
        {"netbox_id": f"8{token}", "name": existing.asset_id, "ip": "10.88.0.6", "type": "R640", "status": "active"},
    ]
    monkeypatch.setattr("app.settings.Settings.netbox_enabled", True)
    monkeypatch.setattr("app.netbox.list_devices", lambda *_a, **_k: devices)
    result = sync_netbox(db)
    assert result["created"] == 1
    assert result["linked"] == 1
    db.expire_all()
    new = db.query(Asset).filter_by(netbox_id=f"9{token}").one()
    assert new.asset_id == f"core-sw-{token}"
    assert new.type == AUTO_ASSET_TYPE
    assert new.scrape_address == ""
    assert new.monitoring_profile == ""
    assert "9100" not in (new.scrape_address or "")
    assert "site=DC1" in new.notes and "tenant=Infra" in new.notes and "tags=core" in new.notes
    kept = db.query(Asset).filter_by(asset_id=existing.asset_id).one()
    assert kept.owner == "payments"
    assert kept.contact_name == "Milan"
    assert kept.owner_email == "milan@dc.local"
    assert kept.notes == "Operator note"
    assert kept.netbox_id == f"8{token}"
    from app.inventory import sd_targets

    assert not any(item["labels"]["asset"] == new.asset_id for item in sd_targets(db))
    db.close()


def test_netbox_asset_id_is_slug_normalized():
    assert netbox_asset_id("Core SW_01 (DC1)") == "core-sw-01-dc1"
    assert netbox_asset_id("", "42") == "nb-42"
    assert len(netbox_asset_id("x" * 100)) == 63


def test_escalation_recipient_rules(monkeypatch):
    db = _db()
    sent: list[str] = []
    monkeypatch.setattr("app.settings.Settings.email_enabled", True)
    monkeypatch.setattr("app.settings.Settings.smtp_host", "smtp.example.test")
    monkeypatch.setattr("app.services._send_smtp", lambda target, *_a, **_k: sent.append(target))

    nobody = _host(db, f"esc-none-{uuid4().hex[:6]}")
    incident = ingest_alertmanager(db, _alert("HighCPU", nobody.asset_id))[0]
    note = db.query(Notification).filter_by(incident_id=incident.id, step_key="immediate").one()
    assert note.status == NO_RECIPIENT_STATUS
    assert "@" not in note.target
    assert sent == []

    owned = _host(db, f"esc-own-{uuid4().hex[:6]}", owner_email="owner@dc.local")
    incident = ingest_alertmanager(db, _alert("HighCPU", owned.asset_id))[0]
    note = db.query(Notification).filter_by(incident_id=incident.id, step_key="immediate").one()
    assert note.status == "sent" and note.target == "owner@dc.local"
    assert sent == ["owner@dc.local"]

    explicit = ensure_notification(db, incident, "45m", target="oncall@dc.local")
    assert explicit.target == "oncall@dc.local"
    assert sent[-1] == "oncall@dc.local"
    assert escalation_recipient(incident, "team-lead") == "owner@dc.local"
    assert escalation_recipient(incident, "boss@dc.local") == "boss@dc.local"
    db.close()


def test_escalation_policy_step_address_reaches_process_escalations(monkeypatch):
    from datetime import timedelta

    from app.models import utcnow
    from app.services import process_escalations

    db = _db()
    slug = f"addr-{uuid4().hex[:6]}"
    policy = EscalationPolicy(name=slug, slug=slug, steps=[{"after_minutes": 0, "target": "team"}, {"after_minutes": 5, "target": "night@dc.local"}])
    db.add(policy)
    db.flush()
    alertname = f"Addr{uuid4().hex[:6]}"
    db.add(Playrule(name=slug, enabled=True, severity="warning", condition={"alertname": alertname}, escalation_policy_id=policy.id))
    db.commit()
    host = _host(db, f"addr-{uuid4().hex[:6]}", owner_email="day@dc.local")
    incident = ingest_alertmanager(db, _alert(alertname, host.asset_id))[0]
    incident.started_at = utcnow() - timedelta(minutes=10)
    incident.ack_at = None
    db.commit()
    process_escalations(db)
    rows = {row.step_key: row.target for row in db.query(Notification).filter_by(incident_id=incident.id)}
    assert rows["immediate"] == "day@dc.local"
    assert rows["5m"] == "night@dc.local"
    db.close()


def test_approve_uses_candidate_hostname_for_asset_id():
    db = _db()
    ip = f"10.91.{uuid4().int % 200}.{uuid4().int % 200 + 1}"
    host = f"Web-{uuid4().hex[:5]}"
    row = DiscoveryCandidate(ip=ip, hostname=host, proposed_role="Possible web/appliance", open_ports=[443], status="new")
    db.add(row)
    db.commit()
    asset = approve_candidate(db, row, actor="tester")
    assert asset.asset_id == host.lower()
    other_ip = f"10.92.{uuid4().int % 200}.{uuid4().int % 200 + 1}"
    plain = DiscoveryCandidate(ip=other_ip, proposed_role="Possible web/appliance", open_ports=[443], status="new")
    db.add(plain)
    db.commit()
    assert approve_candidate(db, plain, actor="tester").asset_id.startswith("disc-")
    db.close()


def test_removed_demo_candidate_is_not_reseeded():
    from app.demo_ids import DEMO_CANDIDATE_IP

    db = _db()
    row = db.query(DiscoveryCandidate).filter_by(ip=DEMO_CANDIDATE_IP).first() or seed_demo_candidate(db)
    delete_candidate(db, row, actor="tester")
    assert seed_demo_candidate(db) is None
    assert db.query(DiscoveryCandidate).filter_by(ip=DEMO_CANDIDATE_IP).first() is None
    db.query(AuditLog).filter(AuditLog.action == "discovery.remove", AuditLog.object_id == DEMO_CANDIDATE_IP).delete()
    db.commit()
    assert seed_demo_candidate(db) is not None
    db.close()


def test_analyst_templates_say_the_honest_thing(monkeypatch):
    db = _db()
    client = TestClient(app)
    _login(client)
    esc = client.get("/escalation")
    assert "no-recipient" in esc.text
    assert "does not invent" in esc.text
    assert "falls back to" not in esc.text
    monkeypatch.setattr("app.settings.Settings.netbox_enabled", True)
    monkeypatch.setattr("app.settings.Settings.netbox_auto_sync", True)
    disc = client.get("/discovery")
    assert "every 6 h" in disc.text
    assert "Auto" in disc.text
    monkeypatch.setattr("app.settings.Settings.netbox_auto_sync", False)
    assert "Manual only" in client.get("/discovery").text
    incident = db.query(Incident).filter(Incident.playbook_id.isnot(None)).order_by(Incident.id.desc()).first()
    if incident is not None:
        page = client.get(f"/incidents/{incident.number}")
        assert "Guidance only" in page.text
        assert "playbook-guide" in page.text
    base = (ROOT / "frontend" / "templates" / "base.html").read_text(encoding="utf-8")
    assert "app.css?v=v08-17" in base
    for name in ["incident_detail.html", "asset_detail.html", "_asset_form.html", "escalation.html"]:
        text = (ROOT / "frontend" / "templates" / name).read_text(encoding="utf-8")
        assert "falls back to policy role@forgesre.local" not in text
    db.close()
