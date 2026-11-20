# Discovery: devices come from LibreNMS

Since 2.0, ForgeSRE never scans a network itself. LibreNMS (bundled, or your own) finds the
devices; ForgeSRE reads the list LibreNMS keeps and decides what to do with each device.

Why: LibreNMS already speaks SNMP v1/v2c/v3 to thousands of device types, follows LLDP/CDP/ARP
neighbours, and knows which addresses it may touch. One scanner on the network is easier to
allow in firewalls and easier to explain than two.

## How it works

1. **LibreNMS discovers.** It adds devices from the ranges you give it (`nets`) when its
   snmp-scan runs, and neighbours of devices it already polls (LLDP, CDP, OSPF, BGP, ARP).
2. **ForgeSRE reads.** Every 6 hours (and on **Read now**) the `discovery_scan` job reads
   LibreNMS's devices and LLDP/CDP links (`app/topology.py`, `app/librenms_discovery.py`):
   - a device that matches an asset (serial, name, sysName, IP) only updates what LibreNMS knows
     about that asset (Inventory › Sources); nothing an operator typed is overwritten;
   - a new device waits in **Inventory › Add devices › From LibreNMS** with a proposed type
     (switch, firewall, printer, server …) until an Engineer presses **Add** or **Ignore**;
   - an ignored address stays ignored on later reads.
3. **Add** creates the asset (`source=librenms`) and runs the monitoring check. Network devices
   are then walked by ForgeSRE's own snmp_exporter, servers need node_exporter or
   windows_exporter (set the scrape address on the asset).

## Settings

| Setting | Meaning |
|---|---|
| `discovery.enabled` | Read new devices from LibreNMS (known assets are always matched) |
| `discovery.mode` | `semi-automatic` (default): read every 6 h, list new devices · `automatic`: add them to the inventory at once · `manual`: only on **Read now** |
| `inventory.librenms.*`, `LIBRENMS_API_TOKEN` | Where LibreNMS is and the API token (`./forgesre settings set-secret LIBRENMS_API_TOKEN`) |

`discovery.cidrs` is gone. `./forgesre update` hands any ranges still in it to LibreNMS and
removes the key.

## From the shell

```bash
./forgesre discovery                              # ready?, mode, last read, found list
./forgesre discovery nets 10.20.30.0/24 10.20.40.0/24  # ranges LibreNMS may discover
./forgesre discovery scan                         # LibreNMS snmp-scan now (up to 30 min), then read
./forgesre discovery read                         # read LibreNMS now (background job)
```

`nets` and `scan` run inside the LibreNMS container (`lnms config:set nets`, `snmp-scan.py`).
LibreNMS uses the SNMP credentials set in its own settings (Global Settings › Poller › SNMP).
Install can pass ranges with `./install.sh --discovery-cidrs 10.20.30.0/24`.

## One device by hand

**Add devices › One device** still asks a single address the operator typed (TCP ports, an SNMP
GET, exporter detection). That is not a sweep. Printers are never sent anything on TCP 9100
(`app/printer_guard.py`).

## When something is wrong

- `./forgesre discovery` says **not ready**: LibreNMS is off or has no API token.
- `./forgesre doctor` → Discovery is yellow with the same reason, or the last read failed
  (`./forgesre journal discovery`).
- Nothing new appears: check the ranges (`./forgesre discovery nets`), that LibreNMS can reach
  UDP/161 on those devices, and its SNMP credentials; then `./forgesre discovery scan`.
