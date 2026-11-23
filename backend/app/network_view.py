"""Network panel: busiest interfaces, BGP peers and OSPF neighbours from SNMP.

Interface traffic comes from the ``if_mib`` job (ifHCIn/OutOctets). BGP and OSPF come from
the ``routing`` module, Prometheus job ``forgesre-snmp-routing``: Router, Firewall and Network
device types by default, any other SNMP asset with extras ``snmp_routing=on`` (``off`` opts a
router out). Every query has the 2 s Prometheus timeout of ``dc_noc.query_prometheus_vector``.
"""

from __future__ import annotations

from typing import Any, Callable

from sqlalchemy.orm import Session

ROUTING_MODULE = "routing"
ROUTING_JOB = "forgesre-snmp-routing"
ROUTING_KEY = "snmp_routing"
ROUTING_TYPES = frozenset({"Router", "Firewall", "Network device"})
BGP_STATES = {1: "idle", 2: "connect", 3: "active", 4: "opensent", 5: "openconfirm", 6: "established"}
OSPF_STATES = {1: "down", 2: "attempt", 3: "init", 4: "2-way", 5: "exstart", 6: "exchange", 7: "loading", 8: "full"}

_J = f'job="{ROUTING_JOB}"'
QUERIES = {
    "in": 'topk(10, rate(ifHCInOctets{job="forgesre-snmp"}[5m]) * 8)',
    "out": 'topk(10, rate(ifHCOutOctets{job="forgesre-snmp"}[5m]) * 8)',
    "bgp_state": f"bgpPeerState{{{_J}}}",
    "bgp_admin": f"bgpPeerAdminStatus{{{_J}}}",
    "bgp_as": f"bgpPeerRemoteAs{{{_J}}}",
    "bgp_up": f"bgpPeerFsmEstablishedTime{{{_J}}}",
    "ospf": f"ospfNbrState{{{_J}}}",
}


def _extras(asset: Any) -> dict:
    raw = asset.get("extras") if isinstance(asset, dict) else getattr(asset, "extras", None)
    return raw if isinstance(raw, dict) else {}


def routing_metrics_on(asset: Any) -> bool:
    """Router / Firewall / Network device unless extras snmp_routing=off; others when it is on."""
    flag = str(_extras(asset).get(ROUTING_KEY) or "").strip().lower()
    if flag in {"0", "off", "false", "no"}:
        return False
    if flag in {"1", "on", "true", "yes"}:
        return True
    type_ = str((asset.get("type") if isinstance(asset, dict) else getattr(asset, "type", "")) or "").strip()
    return type_ in ROUTING_TYPES


def _iface(labels: dict) -> str:
    return str(labels.get("ifName") or labels.get("ifDescr") or f"ifIndex {labels.get('ifIndex', '?')}")


def overview(db: Session, vector_fn: Callable[[str], dict] | None = None) -> dict[str, Any]:
    """Top 10 interfaces each way (bits/s), BGP peers (not established first), OSPF neighbours."""
    if vector_fn is None:
        from app.dc_noc import query_prometheus_vector as vector_fn
    got: dict[str, list[dict]] = {}
    for key, expr in QUERIES.items():
        res = vector_fn(expr)
        if res.get("error"):
            return {"top_in": [], "top_out": [], "bgp": [], "ospf": [], "error": str(res["error"])}
        got[key] = list(res.get("series") or [])

    def top(key: str) -> list[dict[str, Any]]:
        rows = [{"asset": str(s["labels"].get("asset") or s["labels"].get("instance") or ""), "interface": _iface(s["labels"]),
                 "alias": str(s["labels"].get("ifAlias") or ""), "bps": round(s["value"])} for s in got[key]]
        return sorted(rows, key=lambda r: -r["bps"])

    def by_peer(key: str) -> dict[tuple[str, str], float]:
        return {(str(s["labels"].get("asset") or ""), str(s["labels"].get("bgpPeerRemoteAddr") or "")): s["value"] for s in got[key]}

    admin, remote_as, up = by_peer("bgp_admin"), by_peer("bgp_as"), by_peer("bgp_up")
    bgp = []
    for (asset, peer), value in by_peer("bgp_state").items():
        state = BGP_STATES.get(int(value), str(int(value)))
        enabled = int(admin.get((asset, peer), 2)) == 2
        bgp.append({"asset": asset, "peer": peer, "remote_as": int(remote_as.get((asset, peer), 0)), "state": state,
                    "enabled": enabled, "established_s": int(up.get((asset, peer), 0)),
                    "ok": state == "established" or not enabled})
    bgp.sort(key=lambda r: (r["ok"], r["asset"], r["peer"]))
    ospf = []
    for s in got["ospf"]:
        state = OSPF_STATES.get(int(s["value"]), str(int(s["value"])))
        ospf.append({"asset": str(s["labels"].get("asset") or ""), "neighbour": str(s["labels"].get("ospfNbrIpAddr") or ""),
                     "state": state, "ok": state in {"full", "2-way"}})
    ospf.sort(key=lambda r: (r["ok"], r["asset"], r["neighbour"]))
    return {"top_in": top("in"), "top_out": top("out"), "bgp": bgp, "ospf": ospf, "error": ""}
