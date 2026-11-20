"""A printer's TCP 9100 is its raw print port: ForgeSRE must never connect there (roadmap known bug)."""

from __future__ import annotations

import asyncio
import socket
from uuid import uuid4

import pytest

from app import printer_guard
from app.db import Base, SessionLocal, engine
from app.models import Asset
from app.seed import seed

PRINTER = "192.0.2.91"


@pytest.fixture
def net(monkeypatch):
    """Fake network: records every TCP connect; only ports in ``open`` accept."""
    calls: list[tuple[str, int]] = []
    state = {"open": set()}

    def create_connection(address, timeout=None, *args, **kwargs):
        host, port = address[0], int(address[1])
        calls.append((host, port))
        if (host, port) in state["open"]:
            class _Sock:
                def __enter__(self):
                    return self

                def __exit__(self, *exc):
                    return False

                def close(self):
                    return None
            return _Sock()
        raise ConnectionRefusedError(f"refused {host}:{port}")

    async def open_connection(host, port, *args, **kwargs):
        calls.append((host, int(port)))
        raise ConnectionRefusedError(f"refused {host}:{port}")

    monkeypatch.setattr(socket, "create_connection", create_connection)
    monkeypatch.setattr(asyncio, "open_connection", open_connection)
    printer_guard.forget()
    yield calls, state
    printer_guard.forget()


def test_snmp_printer_is_never_sent_anything_on_9100(net, monkeypatch):
    import discovery
    from app import onboarding
    from app.exporter_detect import detect_exporter, fetch_metrics

    calls, state = net
    state["open"] = {(PRINTER, 80), (PRINTER, 443), (PRINTER, 9100)}
    monkeypatch.setattr(printer_guard, "snmp_kind", lambda ip, community="", timeout=1.0: "printer")
    monkeypatch.setattr(discovery, "probe_snmp_udp", lambda *a, **k: True)
    monkeypatch.setattr("app.snmp_get.system_info", lambda *a, **k: {"sys_descr": "HP LaserJet MFP M428", "sys_name": "prn-2nd"})

    found = discovery.probe_host(PRINTER)
    assert 9100 not in found["open_ports"]
    onboarding.identify(PRINTER)
    detect_exporter(PRINTER, timeout=0.2)
    status, _body, why = fetch_metrics(f"http://{PRINTER}:9100/metrics", 0.2)
    assert status is None and "raw print port" in why
    assert (PRINTER, 9100) not in calls
    assert printer_guard.blocked(PRINTER, 9182) == ""


def test_no_snmp_needs_ssh_before_9100(net, monkeypatch):
    calls, state = net
    monkeypatch.setattr(printer_guard, "snmp_kind", lambda ip, community="", timeout=1.0: "")
    assert "Set the type to Linux Server" in printer_guard.blocked("192.0.2.92", 9100)
    state["open"] = {("192.0.2.93", 22)}
    assert printer_guard.blocked("192.0.2.93", 9100) == ""
    state["open"] = {("192.0.2.94", 631), ("192.0.2.94", 22)}
    assert "print port 631" in printer_guard.blocked("192.0.2.94", 9100)
    assert printer_guard.blocked("127.0.0.1", 9100) == ""
    assert not [c for c in calls if c[1] == 9100]


def test_inventory_type_decides_and_printers_never_reach_prometheus(net, monkeypatch):
    from app.inventory import sd_targets

    asked: list[str] = []
    monkeypatch.setattr(printer_guard, "snmp_kind", lambda ip, community="", timeout=1.0: asked.append(ip) or "other")
    Base.metadata.create_all(bind=engine)
    db = SessionLocal()
    seed(db)
    tag = "".join(chr(97 + int(c, 16) % 26) for c in uuid4().hex[:6])
    prn = Asset(asset_id=f"prn-{tag}", hostname=f"prn-{tag}", ip="192.0.2.95", type="Printer", scrape_address="192.0.2.95:9100")
    lnx = Asset(asset_id=f"lnx-{tag}", hostname=f"lnx-{tag}", ip="192.0.2.96", type="Linux Server", scrape_address="192.0.2.96:9100")
    db.add_all([prn, lnx])
    db.commit()
    try:
        assert "printer in the inventory" in printer_guard.blocked("192.0.2.95", 9100)
        assert printer_guard.blocked("192.0.2.96", 9100) == ""
        assert asked == []
        addresses = [t["targets"][0] for t in sd_targets(db)]
        assert "192.0.2.96:9100" in addresses and "192.0.2.95:9100" not in addresses
        lnx.type = "Printer"  # a changed type is decided again
        db.commit()
        assert "printer" in printer_guard.blocked("192.0.2.96", 9100)
    finally:
        db.delete(prn)
        db.delete(lnx)
        db.commit()
        db.close()
