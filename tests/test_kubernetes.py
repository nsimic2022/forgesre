"""Kubernetes / OpenShift source: assets, alerts and deploy changes from a fake API (no network)."""

from __future__ import annotations

from uuid import uuid4

import httpx

from app import kubernetes as k8s
from app.db import Base, SessionLocal, engine
from app.models import Asset, ChangeEvent


def _api(state):
    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if state.get("down"):
            raise httpx.ConnectError("refused")
        if path == "/version":
            return httpx.Response(200, json={"gitVersion": "v1.31.2+k3s1"})
        if path == "/api/v1/nodes":
            return httpx.Response(200, json={"items": [
                {"metadata": {"name": "n1"}, "status": {"addresses": [{"type": "InternalIP", "address": "10.40.0.11"}], "conditions": [{"type": "Ready", "status": "True"}]}},
                {"metadata": {"name": "n2"}, "status": {"addresses": [], "conditions": [{"type": "Ready", "status": "False", "message": "kubelet stopped"}]}},
            ]})
        if path == "/apis/apps/v1/deployments":
            return httpx.Response(200, json={"items": [{"metadata": {"namespace": "erp", "name": "api"}, "spec": {"replicas": 3, "template": {"spec": {"containers": [{"image": state["image"]}]}}}, "status": {"readyReplicas": state.get("ready", 3)}}]})
        if path in ("/apis/apps/v1/statefulsets", "/apis/apps/v1/daemonsets"):
            return httpx.Response(200, json={"items": []})
        if path == "/api/v1/pods":
            pods = [{"metadata": {"namespace": "erp", "name": "api-7d9f-abcde", "ownerReferences": [{"kind": "ReplicaSet", "name": "api-7d9f"}]},
                     "status": {"containerStatuses": [{"state": {"waiting": {"reason": "CrashLoopBackOff"}}}]}}] if state.get("crash") else []
            return httpx.Response(200, json={"items": pods})
        if path == "/apis/config.openshift.io/v1/clusteroperators":
            if not state.get("openshift"):
                return httpx.Response(404, json={})
            return httpx.Response(200, json={"items": [{"metadata": {"name": "ingress"}, "status": {"conditions": [{"type": "Degraded", "status": "True", "message": "router down"}]}}]})
        return httpx.Response(404, json={})

    cluster = {"name": state["name"], "url": "https://k8s.test", "ca_file": "", "ca_pem": "", "insecure": False}
    return cluster, k8s.Api(cluster, client=httpx.Client(base_url="https://k8s.test", transport=httpx.MockTransport(handler)))


def _by(alerts, name, asset_suffix=""):
    return [a for a in alerts if a["labels"]["alertname"] == name and a["labels"]["asset"].endswith(asset_suffix)]


def test_sync_builds_assets_alerts_and_deploy_changes():
    Base.metadata.create_all(bind=engine)
    db = SessionLocal()
    state = {"name": f"c{uuid4().hex[:6]}", "image": "erp/api:4.2.0", "crash": True, "ready": 1, "openshift": True}
    cluster, api = _api(state)
    out = k8s.sync_cluster(db, cluster, api)
    db.commit()
    root = db.query(Asset).filter_by(asset_id=f"k8s-{state['name']}").one()
    assert root.type == k8s.CLUSTER and "v1.31.2+k3s1" in root.notes
    node = db.query(Asset).filter_by(asset_id=f"k8s-{state['name']}-node-n1").one()
    assert node.ip == "10.40.0.11" and node.parent_id == root.id and not node.scrape_address
    wl = db.query(Asset).filter_by(asset_id=f"k8s-{state['name']}-erp-api").one()
    assert wl.hostname == "erp/api" and wl.type == k8s.WORKLOAD and not wl.ip and wl.extras["customer"] == "erp"
    alerts = out["alerts"]
    assert _by(alerts, "KubeApiUnreachable")[0]["status"] == "resolved"
    assert _by(alerts, "KubeNodeNotReady", "node-n2")[0]["status"] == "firing"
    assert _by(alerts, "KubeNodeNotReady", "node-n1")[0]["status"] == "resolved"
    assert _by(alerts, "KubeWorkloadUnavailable")[0]["annotations"]["description"] == "1/3 ready"
    crash = _by(alerts, "KubePodCrashLooping", "erp-api")[0]
    assert crash["status"] == "firing" and "CrashLoopBackOff" in crash["annotations"]["description"]
    op = _by(alerts, "OpenShiftClusterOperatorDegraded")[0]
    assert op["status"] == "firing" and op["labels"]["operator"] == "ingress"

    state.update(image="erp/api:4.2.1", crash=False, ready=3, openshift=False)
    out = k8s.sync_cluster(db, cluster, api)
    db.commit()
    change = db.query(ChangeEvent).filter(ChangeEvent.title.like(f"Deployment erp/api%")).order_by(ChangeEvent.id.desc()).first()
    assert change is not None and change.kind == "deploy" and "4.2.1" in change.title and "4.2.0" in change.detail
    assert _by(out["alerts"], "KubePodCrashLooping", "erp-api")[0]["status"] == "resolved"
    assert not _by(out["alerts"], "OpenShiftClusterOperatorDegraded")  # plain Kubernetes: no operators

    state["down"] = True
    out = k8s.sync_cluster(db, cluster, api)
    assert out["error"].startswith("ConnectError") and _by(out["alerts"], "KubeApiUnreachable")[0]["status"] == "firing"
    db.close()


def test_token_keys_config_and_cli_secret_names():
    from app.cli_ops import format_k8s, write_secret
    from app.settings import settings

    assert k8s.token_key("prod-ocp.1") == "K8S_TOKEN_PROD_OCP_1"
    old = settings.yaml
    try:
        settings.yaml = {**(old or {}), "kubernetes": {"clusters": [{"name": "a", "url": "https://a:6443/"}, {"name": "", "url": "x"}]}}
        assert k8s.clusters() == [{"name": "a", "url": "https://a:6443", "ca_file": "", "ca_pem": "", "insecure": False}]
    finally:
        settings.yaml = old
    import tempfile
    from pathlib import Path

    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "s.env"
        write_secret(path, "K8S_TOKEN_PROD", "abc")
        assert "K8S_TOKEN_PROD=abc" in path.read_text()
    assert "No cluster configured" in format_k8s([])
