"""Inventory › Tree: site → row → rack → asset, children under their parent, worst state per node."""

from __future__ import annotations

from uuid import uuid4

from fastapi.testclient import TestClient

from app.db import Base, SessionLocal, engine
from app.inventory_view import inventory_tree
from app.main import create_app
from app.models import Asset
from app.seed import seed


def _find(node, kind, name):
    if node["kind"] == kind and node["name"] == name:
        return node
    for child in node["children"]:
        hit = _find(child, kind, name)
        if hit:
            return hit
    return None


def test_tree_groups_by_location_and_parent():
    Base.metadata.create_all(bind=engine)
    db = SessionLocal()
    seed(db)
    tag = uuid4().hex[:6]
    site = f"DC-{tag}"
    host = Asset(asset_id=f"esx{tag}", hostname=f"esx{tag}", type="Hypervisor", extras={"site": site, "row": "A", "rack": "A01"}, ping_status="green")
    db.add(host)
    db.flush()
    vm = Asset(asset_id=f"vm{tag}", hostname=f"vm{tag}", type="Linux Server", parent_id=host.id, extras={"site": "Elsewhere"}, ping_status="red")
    loose = Asset(asset_id=f"loose{tag}", hostname=f"loose{tag}", type="Switch", extras={"site": site})
    db.add_all([vm, loose])
    db.commit()
    tree = inventory_tree(db)
    dc = _find(tree, "site", site)
    assert dc is not None and dc["assets"] == 3 and dc["worst"] == "down"
    rack = _find(dc, "rack", "A01")
    esx = _find(rack, "asset", f"esx{tag}")
    assert [c["name"] for c in esx["children"]] == [f"vm{tag}"]  # the VM sits under its hypervisor, not under "Elsewhere"
    assert _find(tree, "site", "Elsewhere") is None or _find(_find(tree, "site", "Elsewhere"), "asset", f"vm{tag}") is None
    assert any(c["kind"] == "asset" and c["name"] == f"loose{tag}" for c in dc["children"])  # no row, no rack: right under the site
    db.close()
    client = TestClient(create_app())
    client.post("/login", data={"email": "admin@forgesre.local", "password": "testpass"})
    body = client.get("/api/v1/inventory/tree").json()
    assert body["kind"] == "root" and body["assets"] >= 3
