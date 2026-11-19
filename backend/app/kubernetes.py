"""Kubernetes, OpenShift and k3s as an optional source (read-only).

For every cluster in ``kubernetes.clusters`` (name, url, optional ca_file / insecure), the
``k8s_sync`` job (leader, every 5 minutes) reads the API with the bearer token
``K8S_TOKEN_<NAME>`` from secrets.env and:

- keeps assets in step: the cluster ("Kubernetes cluster"), its nodes ("Kubernetes node")
  and its Deployments / StatefulSets / DaemonSets ("Kubernetes workload", one per
  namespace/name). Nodes and workloads get the cluster as parent, so when the API is
  unreachable one incident covers them all. Workloads carry no IP and no scrape address,
  so nothing tries to ping or scrape them.
- raises alerts through the normal intake: KubeApiUnreachable, KubeNodeNotReady,
  KubeWorkloadUnavailable, KubePodCrashLooping (CrashLoopBackOff / ImagePullBackOff) and,
  on OpenShift, OpenShiftClusterOperatorDegraded. Healthy again resolves them.
- records an image change on a workload as a "deploy" change, so it shows under
  "What changed before" on incidents.

Only GET requests, 10 s timeout each. The service account needs list/get on nodes, pods,
namespaces, the apps/v1 workloads and (OpenShift) clusteroperators; see docs/kubernetes.md.
"""

from __future__ import annotations

import re
from typing import Any

import httpx
from sqlalchemy.orm import Session

from app.models import Asset
from app.settings import settings

TIMEOUT = 10.0
CLUSTER, NODE, WORKLOAD = "Kubernetes cluster", "Kubernetes node", "Kubernetes workload"
KINDS = (("deployments", "Deployment"), ("statefulsets", "StatefulSet"), ("daemonsets", "DaemonSet"))
BAD_WAITING = {"CrashLoopBackOff", "ImagePullBackOff", "ErrImagePull", "CreateContainerConfigError"}


def clusters() -> list[dict[str, Any]]:
    raw = ((settings.yaml or {}).get("kubernetes") or {}).get("clusters") or []
    out = []
    for item in raw if isinstance(raw, list) else []:
        if isinstance(item, dict) and item.get("name") and item.get("url"):
            out.append({"name": str(item["name"]).strip(), "url": str(item["url"]).rstrip("/"),
                        "ca_file": str(item.get("ca_file") or ""), "ca_pem": str(item.get("ca_pem") or ""),
                        "insecure": bool(item.get("insecure", False))})
    return out


def token_key(name: str) -> str:
    return "K8S_TOKEN_" + re.sub(r"[^A-Z0-9]+", "_", name.upper()).strip("_")


class Api:
    """Tiny read-only client: GET with bearer, 10 s timeout, JSON."""

    def __init__(self, cluster: dict[str, Any], client: httpx.Client | None = None):
        verify: Any = True
        if cluster.get("insecure"):
            verify = False
        elif cluster.get("ca_pem"):
            import ssl

            verify = ssl.create_default_context(cadata=cluster["ca_pem"])  # the cluster's own CA
        elif cluster.get("ca_file"):
            verify = cluster["ca_file"]
        self.client = client or httpx.Client(base_url=cluster["url"], timeout=TIMEOUT, verify=verify,
                                             headers={"Authorization": f"Bearer {settings._secret(token_key(cluster['name']))}"})

    def get(self, path: str) -> dict[str, Any]:
        response = self.client.get(path)
        response.raise_for_status()
        return response.json()

    def items(self, path: str) -> list[dict[str, Any]]:
        return [i for i in self.get(path).get("items") or [] if isinstance(i, dict)]


def _cond(obj: dict[str, Any], kind: str) -> dict[str, Any]:
    for c in (obj.get("status") or {}).get("conditions") or []:
        if c.get("type") == kind:
            return c
    return {}


def _alert(name: str, target: str, firing: bool, severity: str, detail: str, cluster: str) -> dict[str, Any]:
    return {"status": "firing" if firing else "resolved",
            "labels": {"alertname": name, "asset": target, "severity": severity, "job": "forgesre-kubernetes", "cluster": cluster},
            "annotations": {"summary": f"{name}: {target}", "description": detail}}


def _upsert(db: Session, asset_id: str, hostname: str, kind: str, parent: Asset | None, ip: str = "", extras: dict | None = None) -> Asset:
    row = db.query(Asset).filter_by(asset_id=asset_id).first()
    if row is None:
        row = Asset(asset_id=asset_id, hostname=hostname, ip=ip, type=kind, source="kubernetes", environment="Production")
        db.add(row)
    row.hostname, row.type = hostname, kind
    if ip:
        row.ip = ip
    if parent is not None:
        db.flush()  # parent and row need ids
        if parent.id != row.id:
            row.parent_id = parent.id
    if extras is not None:
        merged = dict(row.extras or {})
        merged.update(extras)
        row.extras = merged
    return row


def _owner(pod: dict[str, Any]) -> tuple[str, str]:
    """(kind, name) of the workload behind a pod; a ReplicaSet maps to its Deployment."""
    for ref in (pod.get("metadata") or {}).get("ownerReferences") or []:
        kind, name = ref.get("kind", ""), ref.get("name", "")
        if kind == "ReplicaSet":
            return "Deployment", name.rsplit("-", 1)[0]
        if kind in ("StatefulSet", "DaemonSet"):
            return kind, name
    return "", ""


def sync_cluster(db: Session, cluster: dict[str, Any], api: Api | None = None) -> dict[str, Any]:
    """Read one cluster and return {"alerts": [...], "counts": {...}}; assets are flushed, not committed."""
    from app.changes import record

    name = cluster["name"]
    root = _upsert(db, f"k8s-{name}", name, CLUSTER, None)
    api = api or Api(cluster)
    try:
        version = api.get("/version")
        nodes = api.items("/api/v1/nodes")
    except (httpx.HTTPError, ValueError) as exc:
        detail = f"{type(exc).__name__}: cannot read {cluster['url']}"
        return {"alerts": [_alert("KubeApiUnreachable", root.asset_id, True, "critical", detail, name)], "counts": {}, "error": detail}
    alerts = [_alert("KubeApiUnreachable", root.asset_id, False, "critical", "API reachable", name)]
    root.notes = f"Kubernetes {version.get('gitVersion', '')}".strip()
    for node in nodes:
        meta = node.get("metadata") or {}
        addrs = {a.get("type"): a.get("address") for a in (node.get("status") or {}).get("addresses") or []}
        asset = _upsert(db, f"k8s-{name}-node-{meta.get('name')}", meta.get("name", ""), NODE, root, ip=addrs.get("InternalIP") or "")
        ready = _cond(node, "Ready").get("status") == "True"
        alerts.append(_alert("KubeNodeNotReady", asset.asset_id, not ready, "critical",
                             "node Ready" if ready else f"node not Ready: {_cond(node, 'Ready').get('message', '')}"[:300], name))
    workloads = 0
    for path, kind in KINDS:
        for obj in api.items(f"/apis/apps/v1/{path}"):
            meta, spec, status = obj.get("metadata") or {}, obj.get("spec") or {}, obj.get("status") or {}
            ns, wname = meta.get("namespace", ""), meta.get("name", "")
            images = sorted({c.get("image", "") for c in ((spec.get("template") or {}).get("spec") or {}).get("containers") or []})
            asset_id = f"k8s-{name}-{ns}-{wname}"[:64]
            existing = db.query(Asset).filter_by(asset_id=asset_id).first()
            before = (existing.extras or {}).get("k8s_images", "") if existing else None
            asset = _upsert(db, asset_id, f"{ns}/{wname}", WORKLOAD, root,
                            extras={"customer": ns, "k8s_kind": kind, "k8s_images": ",".join(images)})
            if before is not None and before != ",".join(images):
                db.flush()
                record(db, title=f"{kind} {ns}/{wname} → {', '.join(images)[:180]}", source="kubernetes", kind="deploy",
                       asset_ref=asset.asset_id, detail=f"previous: {before}"[:500])
            want = int(status.get("desiredNumberScheduled") if kind == "DaemonSet" else spec.get("replicas", 1) or 0)
            ready = int(status.get("numberReady") if kind == "DaemonSet" else status.get("readyReplicas") or 0)
            short = want > 0 and ready < want
            alerts.append(_alert("KubeWorkloadUnavailable", asset.asset_id, short, "high",
                                 f"{ready}/{want} ready" if short else f"{ready}/{want} ready", name))
            workloads += 1
    stuck: dict[str, str] = {}
    for pod in api.items("/api/v1/pods?fieldSelector=status.phase!=Succeeded"):
        for cs in (pod.get("status") or {}).get("containerStatuses") or []:
            reason = ((cs.get("state") or {}).get("waiting") or {}).get("reason", "")
            if reason in BAD_WAITING:
                kind, owner = _owner(pod)
                ns = (pod.get("metadata") or {}).get("namespace", "")
                if owner:
                    stuck[f"k8s-{name}-{ns}-{owner}"[:64]] = f"{reason} in {(pod.get('metadata') or {}).get('name')}"
    db.flush()  # SessionLocal has autoflush off: make new workloads visible to the query
    for asset in db.query(Asset).filter(Asset.parent_id == root.id, Asset.type == WORKLOAD).all():
        alerts.append(_alert("KubePodCrashLooping", asset.asset_id, asset.asset_id in stuck, "high",
                             stuck.get(asset.asset_id, "no pod stuck"), name))
    try:
        operators = api.items("/apis/config.openshift.io/v1/clusteroperators")
    except httpx.HTTPStatusError:
        operators = []  # not OpenShift
    for op in operators:
        opname = (op.get("metadata") or {}).get("name", "")
        degraded = _cond(op, "Degraded").get("status") == "True" or _cond(op, "Available").get("status") == "False"
        detail = f"{opname}: {_cond(op, 'Degraded').get('message') or 'not available'}"[:300] if degraded else f"{opname} ok"
        alert = _alert("OpenShiftClusterOperatorDegraded", root.asset_id, degraded, "high", detail, name)
        alert["labels"]["operator"] = opname  # same fingerprint per cluster: one incident until every operator is fine
        alerts.append(alert)
    return {"alerts": alerts, "counts": {"nodes": len(nodes), "workloads": workloads, "operators": len(operators)}}


def sync_all(db: Session, apis: dict[str, Api] | None = None) -> dict[str, Any]:
    """Every configured cluster, then one intake call. Errors are per cluster, never fatal."""
    from app.journal import report
    from app.services import ingest_alertmanager

    alerts: list[dict[str, Any]] = []
    summary: dict[str, Any] = {}
    for cluster in clusters():
        try:
            result = sync_cluster(db, cluster, (apis or {}).get(cluster["name"]))
        except httpx.HTTPError as exc:
            result = {"alerts": [], "counts": {}, "error": f"{type(exc).__name__}: {str(exc)[:200]}"}
        alerts += result["alerts"]
        summary[cluster["name"]] = result.get("error") or result["counts"]
    db.commit()
    if alerts:
        ingest_alertmanager(db, {"status": "firing", "alerts": alerts}, source="prometheus")
        db.commit()
    report(db, "inventory", "kubernetes.sync", "ok", summary=f"Kubernetes: {summary}"[:300])
    db.commit()
    return summary
