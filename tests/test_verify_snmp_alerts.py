"""LibreNMS / SNMP alert routing: IP match, source pills, network playbook, flap guard."""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from uuid import uuid4

from fastapi.testclient import TestClient

from app.asset_verify import (
    PLAYBOOK_NETWORK,
    PLAYBOOK_OS,
    SOURCE_LIBRENMS,
    SOURCE_PROMETHEUS,
    SOURCE_SNMP,
    SOURCE_ZABBIX,
    alert_instance_ip,
    classify_alert_source,
    classify_playbook_type,
    core_check,
    drop_librenms_flaps,
    flap_skip_reason,
    is_network_or_unmanaged,
    verify_and_route_alert,
)
from app.db import Base, SessionLocal, engine
from app.librenms import librenms_fingerprint, parse_webhook as parse_librenms
from app.main import app
from app.models import Asset, Incident, Playbook
from app.seed import NETWORK_STEPS, seed
from app.services import incident_source_label, ingest_alertmanager
from app.settings import settings

import app.asset_verify as verify_mod


def _db():
    Base.metadata.create_all(bind=engine)
    db = SessionLocal()
    seed(db)
    return db


def _client() -> TestClient:
    return TestClient(app)


def _token() -> str:
    return settings.webhook_token


def test_alert_instance_ip_strips_exporter_port():
    assert alert_instance_ip({"instance": "10.80.1.14:161"}) == "10.80.1.14"
    assert alert_instance_ip({"ip": "10.80.1.15"}) == "10.80.1.15"
    assert alert_instance_ip({"instance": "sw-01:9100"}) == ""
    assert alert_instance_ip({"instance": "127.0.0.1:161"}) == ""


def test_classify_alert_source_librenms_snmp_zabbix_agent():
    assert (
        classify_alert_source(
            {"labels": {"alertname": "Device Down", "source": "librenms"}},
            "librenms",
        )
        == SOURCE_LIBRENMS
    )
    assert (
        classify_alert_source({"labels": {"alertname": "SnmpDeviceUnreachable", "job": "forgesre-snmp"}})
        == SOURCE_SNMP
    )
    assert (
        classify_alert_source({"labels": {"alertname": "NetworkInterfaceDown", "job": "forgesre-snmp"}})
        == SOURCE_SNMP
    )
    assert classify_alert_source({"labels": {"alertname": "Host down"}}, "zabbix") == SOURCE_ZABBIX
    assert (
        classify_alert_source({"labels": {"alertname": "NodeExporterDown", "job": "linux-standard"}})
        == SOURCE_PROMETHEUS
    )


def test_playbook_type_network_vs_os():
    switch = {"type": "Switch", "monitoring_profile": "network-switch"}
    linux = {"type": "Linux Server", "monitoring_profile": "linux-standard"}
    assert is_network_or_unmanaged(switch) is True
    assert is_network_or_unmanaged(linux) is False
    lnms = {"labels": {"alertname": "Device Down", "source": "librenms"}}
    assert classify_playbook_type(lnms, switch, SOURCE_LIBRENMS) == PLAYBOOK_NETWORK
    snmp_down = {"labels": {"alertname": "SnmpDeviceUnreachable", "job": "forgesre-snmp"}}
    assert classify_playbook_type(snmp_down, switch, SOURCE_SNMP) == PLAYBOOK_NETWORK
    host_cpu = {"labels": {"alertname": "SnmpCpuHigh", "job": "forgesre-snmp-host"}}
    assert classify_playbook_type(host_cpu, linux, SOURCE_SNMP) == PLAYBOOK_OS
    node = {"labels": {"alertname": "NodeExporterDown", "job": "linux-standard"}}
    assert classify_playbook_type(node, linux, SOURCE_PROMETHEUS) == PLAYBOOK_OS


def test_flap_skip_only_for_direct_librenms_young_down():
    started = (datetime.now(timezone.utc) - timedelta(seconds=20)).isoformat()
    alert = {
        "startsAt": started,
        "labels": {"alertname": "Device Down", "source": "librenms"},
    }
    assert "flap" in flap_skip_reason(alert, SOURCE_LIBRENMS, webhook_source="librenms").lower()
    assert flap_skip_reason(alert, SOURCE_SNMP, webhook_source="prometheus") == ""
    old = {
        "startsAt": (datetime.now(timezone.utc) - timedelta(minutes=5)).isoformat(),
        "labels": {"alertname": "Device Down"},
    }
    assert flap_skip_reason(old, SOURCE_LIBRENMS, webhook_source="librenms") == ""
    assert flap_skip_reason({"labels": {"alertname": "Device Down"}}, SOURCE_LIBRENMS, webhook_source="librenms") == ""


def test_drop_librenms_flaps_skips_when_udp_answers(monkeypatch):
    monkeypatch.setattr(verify_mod, "_snmp_flap_prober", lambda ip, timeout: True)
    alerts = [
        {"labels": {"alertname": "Device Down", "ip": "10.80.9.1", "source": "librenms"}},
        {"labels": {"alertname": "Interface errors", "ip": "10.80.9.1", "source": "librenms"}},
    ]
    kept = drop_librenms_flaps(alerts)
    assert len(kept) == 1
    assert kept[0]["labels"]["alertname"] == "Interface errors"
    monkeypatch.setattr(verify_mod, "_snmp_flap_prober", lambda ip, timeout: False)
    assert len(drop_librenms_flaps(alerts)) == 2


def test_parse_librenms_webhook_firing_and_recovery():
    firing = parse_librenms(
        {
            "title": "Device Down",
            "hostname": "edge-sw-09",
            "ip": "10.80.2.9",
            "state": "1",
            "severity": "critical",
            "msg": "ICMP unreach",
            "id": "44",
        }
    )
    assert firing["status"] == "firing"
    assert firing["labels"]["source"] == "librenms"
    assert firing["labels"]["ip"] == "10.80.2.9"
    assert firing["forge"]["fingerprint"] == librenms_fingerprint("44", "edge-sw-09", "Device Down")
    recovered = parse_librenms(
        {"title": "Device Down", "hostname": "edge-sw-09", "ip": "10.80.2.9", "state": "0", "id": "44"}
    )
    assert recovered["status"] == "resolved"
    flat = parse_librenms(
        {
            "rule_name": "Device Down",
            "hostname": "10.80.2.19",
            "msg": "ICMP timeout",
            "severity": "critical",
            "status": "1",
        }
    )
    assert flat["labels"]["alertname"] == "Device Down"
    assert flat["labels"]["instance"] == "10.80.2.19"
    assert flat["annotations"]["description"] == "ICMP timeout"
    nested = parse_librenms(
        {
            "rule_name": "Port down",
            "device": {"hostname": "core-sw-1", "ip": "10.80.2.20"},
            "msg": "Gi0/1 down",
        }
    )
    assert nested["labels"]["asset"] == "core-sw-1"
    assert nested["labels"]["ip"] == "10.80.2.20"


def test_ingest_snmp_matches_ip_and_attaches_network_playbook():
    db = _db()
    token = uuid4().hex[:8]
    db.add(
        Asset(
            asset_id=f"sw-ip-{token}",
            hostname=f"sw-ip-{token}",
            ip="10.80.3.21",
            type="Switch",
            monitoring_profile="network-switch",
        )
    )
    db.commit()
    created = ingest_alertmanager(
        db,
        {
            "alerts": [
                {
                    "status": "firing",
                    "labels": {
                        "alertname": "SnmpDeviceUnreachable",
                        "instance": "10.80.3.21:161",
                        "job": "forgesre-snmp",
                        "severity": "warning",
                    },
                    "annotations": {"summary": "SNMP down", "description": "no walk"},
                }
            ]
        },
    )
    assert len(created) == 1
    incident = created[0]
    assert incident.source == "snmp"
    assert incident_source_label(incident) == "SNMP"
    assert incident.asset is not None and incident.asset.ip == "10.80.3.21"
    assert incident.playbook is not None and incident.playbook.slug == "network-unreachable"
    assert "fired via SNMP" in " ".join(item.get("detail", "") for item in (incident.timeline or []))
    db.close()


def test_ingest_librenms_pill_and_network_checklist():
    db = _db()
    token = uuid4().hex[:8]
    db.add(
        Asset(
            asset_id=f"sw-ln-{token}",
            hostname=f"sw-ln-{token}",
            ip="10.80.4.8",
            type="Network device",
            monitoring_profile="network-switch",
        )
    )
    db.commit()
    alert = parse_librenms(
        {
            "title": "Device Down",
            "hostname": "other-name-in-lnms",
            "ip": "10.80.4.8",
            "state": "1",
            "severity": "critical",
            "id": token,
        }
    )
    created = ingest_alertmanager(db, {"alerts": [alert]}, source="librenms")
    assert len(created) == 1
    incident = created[0]
    assert incident.source == "librenms"
    assert incident_source_label(incident) == "LibreNMS"
    assert incident.asset is not None and incident.asset.ip == "10.80.4.8"
    assert incident.playbook is not None and incident.playbook.slug == "network-unreachable"
    steps = {item.get("id") for item in (incident.playbook.steps or [])}
    assert {"port", "optics", "snmp"} <= steps
    db.close()


def test_node_exporter_stays_prometheus_and_os_playbook():
    db = _db()
    token = uuid4().hex[:8]
    db.add(
        Asset(
            asset_id=f"lnx-{token}",
            hostname=f"lnx-{token}",
            ip="10.80.5.3",
            type="Linux Server",
            monitoring_profile="linux-standard",
            scrape_address="10.80.5.3:9100",
        )
    )
    db.commit()
    created = ingest_alertmanager(
        db,
        {
            "alerts": [
                {
                    "status": "firing",
                    "labels": {
                        "alertname": "NodeExporterDown",
                        "asset": f"lnx-{token}",
                        "instance": "10.80.5.3:9100",
                        "job": "linux-standard",
                        "severity": "warning",
                        "up": "0",
                    },
                    "annotations": {"summary": "exporter down"},
                }
            ]
        },
    )
    assert len(created) == 1
    incident = created[0]
    assert incident.source == "prometheus"
    assert incident_source_label(incident) == "Prometheus"
    assert incident.playbook is not None and incident.playbook.slug == "host-unreachable"
    db.close()


def test_librenms_webhook_creates_incident_and_pill(monkeypatch):
    monkeypatch.setattr(verify_mod, "_snmp_flap_prober", lambda ip, timeout: False)
    db = _db()
    token = uuid4().hex[:8]
    db.add(
        Asset(
            asset_id=f"sw-wh-{token}",
            hostname=f"sw-wh-{token}",
            ip="10.80.6.4",
            type="Router",
            monitoring_profile="network-switch",
        )
    )
    db.commit()
    db.close()
    client = _client()
    response = client.post(
        "/api/v1/webhooks/librenms",
        json={
            "title": "Device Down",
            "hostname": f"sw-wh-{token}",
            "ip": "10.80.6.4",
            "state": "1",
            "severity": "critical",
            "id": token,
        },
        headers={"Authorization": f"Bearer {_token()}"},
    )
    assert response.status_code == 202, response.text
    body = response.json()
    assert body["accepted"] is True and body["source"] == "librenms"
    assert body["queued"] == 1
    assert "incidents" not in body
    lookup = SessionLocal()
    try:
        incident = lookup.query(Incident).filter(Incident.fingerprint == f"librenms:{token}:sw-wh-{token}").one()
        number = incident.number
    finally:
        lookup.close()
    client.post("/login", data={"email": "admin@forgesre.local", "password": "testpass"}, follow_redirects=False)
    page = client.get(f"/incidents/{number}")
    assert 'data-source="librenms"' in page.text
    listing = client.get(f"/incidents?q={token}")
    assert 'class="src-pill src-librenms"' in listing.text
    assert client.post("/api/v1/webhooks/librenms", json={"title": "x"}, headers={"Authorization": "Bearer nope"}).status_code == 401


def test_snmp_source_pill_on_incidents_list():
    db = _db()
    token = uuid4().hex[:8]
    db.add(
        Asset(
            asset_id=f"sw-pill-{token}",
            hostname=f"sw-pill-{token}",
            ip="10.80.7.7",
            type="Switch",
            monitoring_profile="network-switch",
        )
    )
    db.commit()
    created = ingest_alertmanager(
        db,
        {
            "alerts": [
                {
                    "status": "firing",
                    "labels": {
                        "alertname": "SnmpDeviceUnreachable",
                        "asset": f"sw-pill-{token}",
                        "instance": "10.80.7.7",
                        "job": "forgesre-snmp",
                    },
                }
            ]
        },
    )
    number = created[0].number
    db.close()
    client = _client()
    client.post("/login", data={"email": "admin@forgesre.local", "password": "testpass"}, follow_redirects=False)
    listing = client.get(f"/incidents?q={token}").text
    assert 'class="src-pill src-snmp"' in listing
    detail = client.get(f"/incidents/{number}").text
    assert 'data-source="snmp"' in detail


def test_verify_and_route_alert_is_async_and_normalizes_librenms(monkeypatch):
    monkeypatch.setattr(verify_mod, "_snmp_flap_prober", lambda ip, timeout: False)
    db = _db()
    token = uuid4().hex[:8]
    db.add(
        Asset(
            asset_id=f"sw-async-{token}",
            hostname=f"sw-async-{token}",
            ip="10.80.8.2",
            type="Firewall",
            monitoring_profile="network-switch",
        )
    )
    db.commit()

    async def _run() -> list[Incident]:
        return await verify_and_route_alert(
            db,
            {
                "title": "Device Down",
                "hostname": f"sw-async-{token}",
                "ip": "10.80.8.2",
                "state": "1",
                "id": f"async-{token}",
            },
            source="librenms",
        )

    created = asyncio.run(_run())
    assert len(created) == 1
    assert created[0].source == "librenms"
    assert created[0].playbook is not None and created[0].playbook.slug == "network-unreachable"
    db.close()


def test_network_playbook_seed_has_port_and_optics():
    db = _db()
    book = db.query(Playbook).filter_by(slug="network-unreachable").one()
    ids = [step.get("id") for step in (book.steps or [])]
    assert "port" in ids and "optics" in ids
    titles = " ".join(step.get("title") or "" for step in NETWORK_STEPS)
    assert "switch port" in titles.lower()
    assert "optical" in titles.lower()
    db.close()


def test_core_check_includes_source_pill():
    none = core_check(incident=None)
    assert none.ok is None
    hit = core_check(
        incident={"number": "INC-9", "status": "OPEN", "title": "SNMP down", "source_label": "SNMP"}
    )
    assert hit.ok is True
    assert "[SNMP]" in hit.detail
    assert "webhook reached Core" in hit.detail


def test_alerts_alias_accepts_flat_librenms_rule_name(monkeypatch):
    monkeypatch.setattr(verify_mod, "_snmp_flap_prober", lambda ip, timeout: False)
    db = _db()
    token = uuid4().hex[:8]
    db.add(
        Asset(
            asset_id=f"sw-flat-{token}",
            hostname=f"sw-flat-{token}",
            ip="10.80.9.5",
            type="Switch",
            monitoring_profile="network-switch",
        )
    )
    db.commit()
    db.close()
    client = _client()
    assert client.post(
        "/api/v1/alerts/librenms",
        json={"rule_name": "Device Down", "hostname": "10.80.9.5", "msg": "no ping"},
        headers={"Authorization": "Bearer nope"},
    ).status_code == 401
    response = client.post(
        "/api/v1/alerts/librenms",
        json={
            "rule_name": "Device Down",
            "hostname": "10.80.9.5",
            "msg": "no ping",
            "severity": "critical",
            "id": token,
        },
        headers={"Authorization": f"Bearer {_token()}"},
    )
    assert response.status_code == 202
    assert response.json()["queued"] == 1
    lookup = SessionLocal()
    try:
        row = lookup.query(Incident).filter(Incident.source == "librenms").order_by(Incident.id.desc()).first()
        assert row is not None
        assert row.asset is not None and row.asset.ip == "10.80.9.5"
        assert row.playbook is not None and row.playbook.slug == "network-unreachable"
        assert "Device Down" in (row.title or "")
    finally:
        lookup.close()
