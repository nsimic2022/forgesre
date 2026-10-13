"""One parent pointer: a down parent suppresses the child down-alert mail ladder."""

from __future__ import annotations

from uuid import uuid4

from fastapi.testclient import TestClient

from app.db import Base, SessionLocal, engine
from app.dc_noc import is_down_style, set_asset_parent
from app.inventory import create_manual_asset
from app.main import app
from app.models import Notification
from app.seed import seed
from app.services import ingest_alertmanager


def _db():
    Base.metadata.create_all(bind=engine)
    db = SessionLocal()
    seed(db)
    return db


def _client() -> TestClient:
    client = TestClient(app)
    client.post("/login", data={"email": "admin@forgesre.local", "password": "testpass"}, follow_redirects=False)
    return client


def _host(db, token: str, kind: str, email: str):
    raw = uuid4().int
    return create_manual_asset(
        db,
        hostname=f"{kind}-{token}",
        asset_id=f"{kind}-{token}",
        ip=f"10.78.{raw % 200 + 1}.{(raw >> 8) % 200 + 20}",
        type="Switch" if kind == "sw" else "Linux Server",
        owner_email=email,
        actor="tester",
    )


def _fire(asset_id: str, name: str, **labels) -> dict:
    body = {"alertname": name, "severity": "warning", "asset": asset_id, "instance": asset_id}
    body.update(labels)
    return {
        "status": "firing",
        "alerts": [
            {
                "status": "firing",
                "labels": body,
                "annotations": {"summary": name, "description": "parent test"},
            }
        ],
    }


def _resolve(asset_id: str, name: str) -> dict:
    return {
        "status": "resolved",
        "alerts": [
            {
                "status": "resolved",
                "labels": {"alertname": name, "severity": "warning", "asset": asset_id, "instance": asset_id},
                "annotations": {"summary": name},
            }
        ],
    }


def _mails(db, incident) -> list[Notification]:
    return db.query(Notification).filter(Notification.incident_id == incident.id).all()


def test_down_style_names():
    assert is_down_style("NetworkInterfaceDown")
    assert is_down_style("SnmpDeviceUnreachable")
    assert is_down_style("NodeExporterDown")
    assert not is_down_style("NodeCPUHigh")
    assert not is_down_style("NodeFilesystemUsageHigh")


def test_parent_down_suppresses_child_ladder_until_parent_clears():
    db = _db()
    token = uuid4().hex[:8]
    parent = _host(db, token, "sw", "parent-owner@dc.local")
    child = _host(db, token + "c", "srv", "child-owner@dc.local")
    assert child.id != parent.id
    set_asset_parent(db, child, parent.asset_id)
    db.commit()
    db.refresh(child)
    assert child.parent_id == parent.id

    parent_name = "SnmpDeviceUnreachable"
    opened = ingest_alertmanager(db, _fire(parent.asset_id, parent_name))
    assert len(opened) == 1
    parent_inc = opened[0]
    assert parent_inc.status == "OPEN"
    assert len(_mails(db, parent_inc)) == 1

    child_name = "NetworkInterfaceDown"
    child_opened = ingest_alertmanager(db, _fire(child.asset_id, child_name, ifName="Gi0/1"))
    assert len(child_opened) == 1
    child_inc = child_opened[0]
    db.refresh(child_inc)
    assert child_inc.status == "SUPPRESSED"
    assert _mails(db, child_inc) == []
    assert parent_inc.number in " ".join(step.get("detail") or "" for step in child_inc.timeline or [])

    cpu = ingest_alertmanager(db, _fire(child.asset_id, f"NodeCPUHigh{token}"))
    assert len(cpu) == 1
    assert cpu[0].status == "OPEN"
    assert len(_mails(db, cpu[0])) == 1

    ingest_alertmanager(db, _resolve(parent.asset_id, parent_name))
    db.refresh(parent_inc)
    assert parent_inc.status == "RESOLVED"
    again = ingest_alertmanager(db, _fire(child.asset_id, child_name, ifName="Gi0/1"))
    assert again == []
    db.refresh(child_inc)
    assert child_inc.status == "OPEN"
    mails = _mails(db, child_inc)
    assert len(mails) == 1
    assert mails[0].target == "child-owner@dc.local"
    db.close()


def test_parent_picker_on_the_asset_form_and_list():
    db = _db()
    token = uuid4().hex[:8]
    parent = _host(db, token, "sw", "parent-owner@dc.local")
    child = _host(db, token + "e", "srv", "child-owner@dc.local")
    client = _client()
    page = client.get(f"/assets?edit={child.asset_id}")
    assert page.status_code == 200
    assert 'name="parent_asset_id"' in page.text
    assert parent.asset_id in page.text
    saved = client.post(
        f"/assets/{child.asset_id}/update",
        data={
            "hostname": child.hostname,
            "ip": child.ip,
            "type": "Linux Server",
            "parent_asset_id": parent.asset_id,
            "owner_email": "child-owner@dc.local",
        },
        follow_redirects=False,
    )
    assert saved.status_code in {302, 303}
    db.expire_all()
    row = db.query(type(child)).filter_by(asset_id=child.asset_id).first()
    assert row.parent_id == parent.id
    listed = client.get(f"/assets?q={child.asset_id}").text
    assert "data-parent-host" in listed
    assert parent.hostname in listed
    detail = client.get(f"/assets/{child.asset_id}").text
    assert f'href="/assets/{parent.asset_id}"' in detail
    db.close()
