"""ForgeSRE → Kubernetes / OpenShift clusters, one lane per cluster (registered in app.flows).

Settings (URL, read-only token set) → DNS/TCP/TLS → /version → nodes readable → pods readable.
Then the last sync. Read-only GETs with the client's 10 s timeout; the token is never shown.
"""

from __future__ import annotations

import time
from typing import Any

import httpx
from sqlalchemy.orm import Session

from app.flows import Flow, journal_step, network_steps, register, step
from app.settings import settings


def _read(api: Any, sid: str, label: str, path: str, what: str) -> tuple[dict[str, Any], Any]:
    started = time.monotonic()
    try:
        data = api.get(path)
    except httpx.HTTPStatusError as exc:
        code = exc.response.status_code
        fix = {401: "The token is wrong or expired.", 403: "The service account may not read this: bind it to the forgesre-reader ClusterRole (docs/kubernetes.md)."}.get(code, "")
        return step(sid, label, path, "bad", f"HTTP {code}.", ms=int((time.monotonic() - started) * 1000), fix=fix), None
    except (httpx.HTTPError, ValueError) as exc:
        return step(sid, label, path, "bad", f"No answer: {exc.__class__.__name__}.", ms=int((time.monotonic() - started) * 1000)), None
    ms = int((time.monotonic() - started) * 1000)
    if isinstance(data, dict) and "items" in data:
        return step(sid, label, path, "ok", f"{len(data['items'] or [])} {what} readable.", ms=ms), data
    return step(sid, label, path, "ok", what, ms=ms), data


def _cluster_lane(cluster: dict[str, Any]) -> list[dict[str, Any]]:
    from app.kubernetes import Api, token_key

    key = token_key(cluster["name"])
    has = bool(settings._secret(key))
    steps = [step("cfg", "Settings", cluster["name"], "ok" if has else "bad", f"{cluster['url']}; token {key} {'set' if has else 'empty'}.",
                  fix="" if has else f"./forgesre settings set-secret {key}")]
    if not has:
        return steps
    net, ok = network_steps(cluster["url"], what=f"the {cluster['name']} API", tls=None if not cluster.get("insecure") else False)
    if cluster.get("insecure"):
        net.append(step("tls", "TLS", "insecure", "warn", "Certificate checks are off for this cluster (insecure: true).",
                        fix="Set ca_file or ca_pem for the cluster and remove insecure."))
    steps += net
    if not ok:
        return steps
    api = Api(cluster)
    try:
        version, data = _read(api, "version", "API", "/version", "answers")
        if data:
            version["detail"] = f"Kubernetes {data.get('gitVersion', '?')}."
        steps.append(version)
        if version["status"] == "ok":
            steps.append(_read(api, "nodes", "Nodes", "/api/v1/nodes?limit=500", "node(s)")[0])
            steps.append(_read(api, "pods", "Pods", "/api/v1/pods?limit=1", "pod(s) on the first page")[0])
    finally:
        api.client.close()
    return steps


def check(db: Session) -> dict[str, Any]:
    """One lane per configured cluster, then the last sync."""
    from app.kubernetes import clusters

    lanes = [{"id": f"c{i}", "title": f"ForgeSRE → {c['name']} (read-only)", "steps": _cluster_lane(c)} for i, c in enumerate(clusters())]
    if not lanes:
        lanes = [{"id": "c0", "title": "ForgeSRE → cluster", "steps": [step("cfg", "Settings", "kubernetes.clusters", "unknown",
                                                                             "No cluster is configured.", fix="docs/kubernetes.md: add a cluster under kubernetes.clusters")]}]
    lanes.append({"id": "sync", "title": "Inside ForgeSRE", "steps": [journal_step(db, "sync", "Last sync", "inventory", "kubernetes.sync",
                                                                                   never="No cluster was read yet.")]})
    return {"lanes": lanes}


def _configured() -> bool:
    from app.kubernetes import clusters

    return bool(clusters())


register(Flow(
    name="kubernetes", label="Kubernetes", check=check, configured=_configured, log_words=("kubernetes", "k8s"),
    about="How ForgeSRE reads nodes and workloads from each cluster with a read-only token.",
))
