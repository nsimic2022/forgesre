"""Shared ForgeRCA families, hypothesis catalogs, and playrule presets.

ForgeRCA (Python builtin) always runs first. ForgeAI is the local LLM rewrite.
Playrule presets match bundled Prometheus/Alertmanager names where they exist.
"""

from __future__ import annotations

from typing import Any

# (id, summary, keywords)
HypothesisRow = tuple[str, str, tuple[str, ...]]


FAMILIES = (
    "windows",
    "firewall",
    "router",
    "switch",
    "snmp",
    "storage",
    "linux-memory",
    "linux-host",
    "linux-disk",
    "linux-cpu",
    "generic",
)

HYPOTHESES: dict[str, list[HypothesisRow]] = {
    "linux-cpu": [
        ("high-process", "High process activity is consuming CPU.", ("cpu", "process", "load")),
        ("runaway-job", "A runaway job or cron task may be burning CPU.", ("cron", "job", "batch")),
        ("noisy-neighbor", "Another tenant or container may be competing for CPU.", ("noisy", "neighbor", "cgroup")),
        ("missing-idle", "CPU stayed elevated rather than returning to baseline.", ("sustained", "elevated")),
        ("kernel-softirq", "Kernel / softirq or interrupt load may be consuming CPU.", ("softirq", "si", "interrupt", "ksoftirq")),
        ("hypervisor-steal", "Hypervisor steal time may be starving this guest.", ("steal", "hypervisor", "vm", "kvm")),
        ("backup-window", "A backup or rsync window may be saturating CPU.", ("backup", "rsync", "borg", "restic")),
        ("compile-build", "A compile or CI build may be burning CPU.", ("compile", "gcc", "make", "npm", "maven")),
        ("security-scan", "An antivirus or vulnerability scan may be burning CPU.", ("clam", "scan", "crowdstrike", "auditd")),
    ],
    "linux-disk": [
        ("log-growth", "Rapid log growth may be consuming disk space.", ("log", "journal", "syslog", "grow")),
        ("database-growth", "Database growth may be consuming disk space.", ("postgres", "mysql", "database", "wal")),
        ("temp-files", "Temporary files may be consuming disk space.", ("tmp", "temp", "cache")),
        ("backup-files", "Backup files may be consuming disk space.", ("backup", "dump", "snapshot")),
        ("app-data", "Application data growth may be consuming disk space.", ("data", "upload", "volume")),
        ("process-activity", "Unexpected process activity may be writing data quickly.", ("write", "i/o", "process")),
        ("inode-exhaustion", "The filesystem may be out of inodes rather than bytes.", ("inode", "no space", "enospc")),
        ("container-overlay", "Container overlay / docker volumes may be filling the disk.", ("docker", "overlay", "containerd", "podman")),
        ("journal-vacuum", "systemd-journal may need vacuuming.", ("journald", "persistent", "vacuum")),
        ("core-dumps", "Core dumps or crash files may be consuming disk space.", ("core", "coredump", "abrtd")),
    ],
    "linux-memory": [
        ("oom-killer", "The OOM killer may have fired or RAM may be exhausted.", ("oom", "killed process", "out of memory")),
        ("memory-leak", "A process may be leaking memory.", ("rss", "leak", "grew")),
        ("page-cache", "Page cache pressure may look like high used memory.", ("cache", "buff/cache", "available")),
        ("swap-thrash", "The host may be thrashing swap.", ("swap", "si", "so", "thrash")),
        ("tmpfs-ram", "A tmpfs or ramdisk may be holding RAM.", ("tmpfs", "dev/shm")),
    ],
    "linux-host": [
        ("node-exporter-down", "node_exporter on TCP/9100 may be down or firewalled from ForgeSRE.", ("9100", "node_exporter", "scrape")),
        ("host-down", "The Linux host may be down or isolated from the management network.", ("down", "unreachable", "timeout", "ping")),
        ("ssh-blocked", "SSH or management access may be blocked (host still up).", ("ssh", "22", "iptables", "nft")),
        ("dns-name", "The scrape address or DNS name may be wrong.", ("dns", "nxdomain", "resolve")),
    ],
    "windows": [
        ("win-cpu", "A Windows process or service may be consuming CPU.", ("cpu", "processor", "w3wp", "sqlservr")),
        ("win-exporter", "windows_exporter on TCP/9182 may be down or firewalled from ForgeSRE.", ("9182", "windows_exporter", "scrape")),
        ("win-service", "A Windows service may have crashed or failed to start.", ("service", "scm", "event id")),
        ("win-disk", "An NTFS volume may be full.", ("disk", "ntfs", "c:", "volume")),
        ("win-update", "Windows Update or a reboot-pending patch may be disrupting the host.", ("update", "reboot", "wuauserv")),
        ("win-iis", "An IIS / application pool may be recycling or hung.", ("iis", "w3wp", "apppool")),
        ("win-eventlog", "The Windows event log may be full or flooding.", ("event log", "eventlog")),
        ("win-defender", "Defender or another AV scan may be saturating the host.", ("defender", "antivirus", "msmpeng")),
    ],
    "storage": [
        ("volume-full", "A volume, LUN, or datastore may be full.", ("lun", "datastore", "volume", "capacity")),
        ("snapshot-grow", "Snapshots may be consuming backend capacity.", ("snapshot", "snap", "shadow")),
        ("raid-degraded", "The array may be degraded or rebuilding.", ("raid", "degraded", "rebuild", "smart")),
        ("multipath", "A SAN/iSCSI path may be down (multipath).", ("multipath", "iscsi", "fc", "path")),
        ("nfs-export", "An NFS/CIFS export may be stale or unmounted.", ("nfs", "cifs", "smb", "stale")),
        ("replica-lag", "Storage replication may be lagging.", ("replica", "lag", "sync")),
    ],
    "switch": [
        ("interface-down", "An interface may be admin-up / oper-down.", ("interface", "ifoper", "ifadmin", "down")),
        ("uplink", "An uplink to the core / distribution may be down.", ("uplink", "trunk", "lag", "port-channel")),
        ("stp-loop", "Spanning-tree or a loop may be disrupting forwarding.", ("stp", "rstp", "loop", "bpdu")),
        ("crc-errors", "CRC / input errors may indicate a bad cable or duplex mismatch.", ("crc", "input error", "duplex")),
        ("vlan-misconfig", "A VLAN or trunk allowed-list may be wrong.", ("vlan", "trunk", "native")),
        ("mac-flap", "MAC flapping may indicate a loop or mis-cabling.", ("flap", "mac", "move")),
    ],
    "router": [
        ("bgp-neighbor", "A BGP neighbor may be down.", ("bgp", "neighbor", "peer")),
        ("igp", "OSPF/IS-IS adjacency may be down.", ("ospf", "isis", "adjacency")),
        ("route-missing", "A prefix or default route may be missing.", ("route", "prefix", "nexthop")),
        ("acl-mgmt", "A management ACL may be blocking SNMP/SSH from ForgeSRE.", ("acl", "management", "vty")),
        ("control-plane", "Control-plane CPU may be high (not just forwarding).", ("control-plane", "cpu")),
        ("wan-circuit", "A WAN circuit or next-hop may be down.", ("wan", "circuit", "serial", "tunnel")),
    ],
    "firewall": [
        ("session-table", "The session / connection table may be full.", ("session", "conntrack", "state table")),
        ("policy-deny", "A security policy may be denying expected traffic.", ("deny", "drop", "policy", "rule")),
        ("vpn-down", "A site-to-site VPN or tunnel may be down.", ("vpn", "ipsec", "ike", "tunnel")),
        ("ha-failover", "Firewall HA may have failed over or split.", ("ha", "failover", "cluster")),
        ("nat-pool", "A NAT pool may be exhausted.", ("nat", "pat", "pool")),
        ("mgmt-acl", "Management ACL may block SNMP/HTTPS from ForgeSRE.", ("acl", "management", "snmp")),
    ],
    "generic": [
        ("alert-matched", "The alert condition matched; inspect evidence before changing anything.", ("alert", "firing")),
        ("config-change", "A recent change may have caused this.", ("change", "deploy", "commit")),
        ("dependency", "A dependency of this asset may be failing.", ("timeout", "refused", "dependency")),
        ("capacity", "A capacity limit may have been crossed.", ("limit", "quota", "capacity")),
    ],
}

SUMMARIES = {
    "linux-cpu": "CPU usage increased rapidly on {hostname}.",
    "linux-disk": "Disk capacity is under pressure on {hostname}.",
    "linux-memory": "Memory pressure increased on {hostname}.",
    "linux-host": "Linux host reachability failed on {hostname}.",
    "windows": "Windows host incident on {hostname}.",
    "storage": "Storage capacity or path issue on {hostname}.",
    "switch": "Switch / interface incident on {hostname}.",
    "router": "Router / routing incident on {hostname}.",
    "firewall": "Firewall or security-appliance incident on {hostname}.",
    "snmp": "SNMP or network reachability failed on {hostname}.",
    "generic": "Incident on {hostname}: {title}.",
}

ACTIONS = {
    "linux-cpu": "Engineer should inspect top CPU processes.",
    "linux-disk": "Engineer should verify disk usage, growth, and the owning team.",
    "linux-memory": "Engineer should inspect memory, OOM logs, and swap on the host.",
    "linux-host": "Engineer should check ping/SSH and node_exporter TCP/9100 from the ForgeSRE host.",
    "windows": "Engineer should check windows_exporter TCP/9182 from the ForgeSRE host, then Task Manager/services and the System event log.",
    "storage": "Engineer should verify volume/LUN capacity, snapshots, and paths. Nothing was executed.",
    "switch": "Engineer should check interface oper status, errors, and uplinks (SNMP if_mib).",
    "router": "Engineer should check routing adjacencies, management ACL, and SNMP from the ForgeSRE host.",
    "firewall": "Engineer should check session table, VPN/HA, and whether management ACL allows SNMP from ForgeSRE.",
    "snmp": "Engineer should check SNMP community, ACL, and UDP/161 from the ForgeSRE host.",
}

# Dropdown values. alertname matches bundled Alertmanager rules when present.
PLAYRULE_PRESETS: list[dict[str, Any]] = [
    {
        "group": "Demo gauges (forge-demo-01)",
        "rules": [
            {"id": "high-cpu", "label": "HighCPU — cpu_usage > 80", "name": "high-cpu", "alertname": "HighCPU", "metric": "cpu_usage", "operator": ">", "value": 80, "severity": "warning", "playbook": "cpu-high"},
            {"id": "high-disk", "label": "FilesystemUsageHigh — filesystem_usage > 80", "name": "high-disk", "alertname": "FilesystemUsageHigh", "metric": "filesystem_usage", "operator": ">", "value": 80, "severity": "warning", "playbook": "disk-full"},
        ],
    },
    {
        "group": "Linux (node_exporter)",
        "rules": [
            {"id": "node-cpu", "label": "NodeCPUHigh — cpu_usage > 95", "name": "node-cpu", "alertname": "NodeCPUHigh", "metric": "cpu_usage", "operator": ">", "value": 95, "severity": "warning", "playbook": "cpu-high"},
            {"id": "node-filesystem", "label": "NodeFilesystemUsageHigh — filesystem_usage > 90", "name": "node-filesystem", "alertname": "NodeFilesystemUsageHigh", "metric": "filesystem_usage", "operator": ">", "value": 90, "severity": "warning", "playbook": "disk-full"},
            {"id": "node-exporter-down", "label": "NodeExporterDown — up == 0", "name": "node-exporter-down", "alertname": "NodeExporterDown", "metric": "up", "operator": "==", "value": 0, "severity": "warning", "playbook": "host-unreachable"},
            {"id": "node-memory", "label": "NodeMemoryHigh — memory_usage > 90 (if you add that alert)", "name": "node-memory", "alertname": "NodeMemoryHigh", "metric": "memory_usage", "operator": ">", "value": 90, "severity": "warning", "playbook": "host-unreachable"},
            {"id": "node-load", "label": "NodeLoadHigh — load > 8 (if you add that alert)", "name": "node-load", "alertname": "NodeLoadHigh", "metric": "load1", "operator": ">", "value": 8, "severity": "warning", "playbook": "cpu-high"},
        ],
    },
    {
        "group": "Network (snmp_exporter if_mib)",
        "rules": [
            {"id": "snmp-down", "label": "SnmpDeviceUnreachable — up == 0", "name": "snmp-down", "alertname": "SnmpDeviceUnreachable", "metric": "up", "operator": "==", "value": 0, "severity": "warning", "playbook": "network-unreachable"},
            {"id": "if-down", "label": "NetworkInterfaceDown — ifOperStatus == 2", "name": "network-if-down", "alertname": "NetworkInterfaceDown", "metric": "ifOperStatus", "operator": "==", "value": 2, "severity": "warning", "playbook": "network-unreachable"},
        ],
    },
    {
        "group": "Windows (windows_exporter)",
        "rules": [
            {"id": "win-cpu", "label": "WindowsCPUHigh — cpu_usage > 90", "name": "windows-cpu", "alertname": "WindowsCPUHigh", "metric": "cpu_usage", "operator": ">", "value": 90, "severity": "warning", "playbook": "cpu-high"},
            {"id": "win-disk", "label": "WindowsFilesystemUsageHigh — filesystem_usage > 90", "name": "windows-filesystem", "alertname": "WindowsFilesystemUsageHigh", "metric": "filesystem_usage", "operator": ">", "value": 90, "severity": "warning", "playbook": "disk-full"},
            {"id": "win-exporter-down", "label": "WindowsExporterDown — up == 0", "name": "windows-exporter-down", "alertname": "WindowsExporterDown", "metric": "up", "operator": "==", "value": 0, "severity": "warning", "playbook": "windows-unreachable"},
        ],
    },
    {
        "group": "Storage (alertname match if Prometheus sends them)",
        "rules": [
            {"id": "storage-lun", "label": "StorageVolumeUsageHigh — filesystem_usage > 90", "name": "storage-volume", "alertname": "StorageVolumeUsageHigh", "metric": "filesystem_usage", "operator": ">", "value": 90, "severity": "warning", "playbook": "disk-full"},
        ],
    },
]


def _blob(context) -> str:
    kind = str((context.asset or {}).get("type") or "").lower()
    profile = str((context.asset or {}).get("monitoring_profile") or "").lower()
    title = str((context.incident or {}).get("title") or "").lower()
    alerts = " ".join(str((alert or {}).get("alertname") or "") for alert in (context.alerts or []))
    return f"{kind} {profile} {title} {alerts}".lower()


def classify_family(context, samples: dict[str, tuple] | None = None) -> str:
    """Pick one ForgeRCA family. CPU/disk demo paths stay linux-cpu / linux-disk."""
    blob = _blob(context)
    samples = samples or {}
    cpu = samples.get("cpu_percent", samples.get("cpu_usage", (None, None)))[0]
    disk = samples.get("disk_percent", samples.get("disk_volume_percent", samples.get("filesystem_usage", (None, None))))[0]
    mem = samples.get("memory_percent", samples.get("memory_usage", (None, None)))[0]
    up = samples.get("up", (None, None))[0]
    disk_alert = any(word in blob for word in ("disk", "file", "filesystem", "volume", "lun", "datastore"))
    cpu_alert = any(word in blob for word in ("cpu", "load", "processor"))
    mem_alert = any(word in blob for word in ("memory", "ram", "oom", "swap"))
    host_alert = any(word in blob for word in ("nodeexporter", "node_exporter", "host down", "host-unreachable"))
    if "nodeexporterdown" in blob.replace(" ", "").replace("_", "") or "nodeexporterdown" in blob.replace("_", ""):
        host_alert = True
    if "node exporter" in blob or "nodeexporter" in blob.replace("_", ""):
        host_alert = True

    if any(word in blob for word in ("windows", "win32", "iis", "wmi", "windows server")):
        return "windows"
    if any(word in blob for word in ("firewall", "asa", "palo", "fortigate", "fortinet", "checkpoint", "nftables", "iptables")):
        return "firewall"
    if any(word in blob for word in ("router", "bgp", "ospf", "isis")):
        return "router"
    if any(word in blob for word in ("switch", "vlan", "stp", "ifoper", "networkinterfacedown")):
        return "switch"
    if any(word in blob for word in ("storage", "lun", "san", "nas", "iscsi", "datastore", "raid", "nfs")) and not cpu_alert:
        return "storage"
    compact = blob.replace(" ", "").replace("_", "").replace("-", "")
    if "nodeexporterdown" in compact:
        return "linux-host"
    if host_alert or (up is not None and up == 0 and not disk_alert and not cpu_alert):
        if any(word in blob for word in ("snmp", "if_mib", "network")):
            return "snmp"
        return "linux-host"
    if any(word in blob for word in ("snmp", "unreachable")) and not disk_alert and not cpu_alert:
        return "snmp"
    diskish = disk_alert or (disk is not None and disk > 80 and not cpu_alert)
    if diskish:
        return "linux-disk"
    if mem_alert or (mem is not None and mem > 90 and not cpu_alert and not disk_alert):
        return "linux-memory"
    cpuish = cpu_alert or (cpu is not None and cpu > 80 and not disk_alert)
    if cpuish:
        return "linux-cpu"
    return "generic"


def hypotheses_for(family: str) -> list[HypothesisRow]:
    return list(HYPOTHESES.get(family) or HYPOTHESES["linux-cpu"])


def summary_for(family: str, hostname: str, title: str = "", alertname: str = "") -> str:
    template = SUMMARIES.get(family)
    if template:
        return template.format(hostname=hostname, title=title or alertname or "alert")
    return f"Incident on {hostname}: {title or alertname or 'alert'}."


def action_for(family: str, playbook: str = "") -> str:
    text = ACTIONS.get(family) or "Engineer should inspect evidence, Grafana, and recent changes."
    if playbook and family in {"linux-disk", "linux-cpu", "snmp", "linux-host", "switch", "router", "firewall"}:
        return f"{text} Follow playbook {playbook}."
    if playbook and family not in ACTIONS:
        return f"Engineer should inspect evidence and follow playbook {playbook} as guidance only."
    return text
