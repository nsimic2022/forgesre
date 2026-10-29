auths:
  public_v2:
    version: 2
    community: __SNMP_COMMUNITY__
  public_v1:
    version: 1
    community: __SNMP_COMMUNITY__
  # forgesre-asset-auths begin (Custom community / SNMPv3 per asset — written by ./forgesre snmp-auths, do not edit)
  # forgesre-asset-auths end

modules:
  # Minimal slice of if_mib from prometheus/snmp_exporter v0.26.0 snmp.yml (generator output).
  # v0.26 has no module-level lookups: each metric carries its own lookups list.
  if_mib:
    timeout: 5s
    retries: 1
    max_repetitions: 25
    walk:
      - 1.3.6.1.2.1.2.2.1.2
      - 1.3.6.1.2.1.2.2.1.7
      - 1.3.6.1.2.1.2.2.1.8
      - 1.3.6.1.2.1.31.1.1.1.1
      - 1.3.6.1.2.1.31.1.1.1.6
      - 1.3.6.1.2.1.31.1.1.1.10
      - 1.3.6.1.2.1.31.1.1.1.18
    get:
      - 1.3.6.1.2.1.1.3.0
    metrics:
      - name: sysUpTime
        oid: 1.3.6.1.2.1.1.3
        type: gauge
        help: The time (in hundredths of a second) since the network management portion of the system was last re-initialized. - 1.3.6.1.2.1.1.3
      - name: ifAdminStatus
        oid: 1.3.6.1.2.1.2.2.1.7
        type: gauge
        help: The desired state of the interface - 1.3.6.1.2.1.2.2.1.7
        indexes:
          - labelname: ifIndex
            type: gauge
        lookups:
          - labels: [ifIndex]
            labelname: ifAlias
            oid: 1.3.6.1.2.1.31.1.1.1.18
            type: DisplayString
          - labels: [ifIndex]
            labelname: ifDescr
            oid: 1.3.6.1.2.1.2.2.1.2
            type: DisplayString
          - labels: [ifIndex]
            labelname: ifName
            oid: 1.3.6.1.2.1.31.1.1.1.1
            type: DisplayString
        enum_values:
          1: up
          2: down
          3: testing
      - name: ifOperStatus
        oid: 1.3.6.1.2.1.2.2.1.8
        type: gauge
        help: The current operational state of the interface - 1.3.6.1.2.1.2.2.1.8
        indexes:
          - labelname: ifIndex
            type: gauge
        lookups:
          - labels: [ifIndex]
            labelname: ifAlias
            oid: 1.3.6.1.2.1.31.1.1.1.18
            type: DisplayString
          - labels: [ifIndex]
            labelname: ifDescr
            oid: 1.3.6.1.2.1.2.2.1.2
            type: DisplayString
          - labels: [ifIndex]
            labelname: ifName
            oid: 1.3.6.1.2.1.31.1.1.1.1
            type: DisplayString
        enum_values:
          1: up
          2: down
          3: testing
          4: unknown
          5: dormant
          6: notPresent
          7: lowerLayerDown
      - name: ifHCInOctets
        oid: 1.3.6.1.2.1.31.1.1.1.6
        type: counter
        help: The total number of octets received on the interface, including framing characters - 1.3.6.1.2.1.31.1.1.1.6
        indexes:
          - labelname: ifIndex
            type: gauge
        lookups:
          - labels: [ifIndex]
            labelname: ifAlias
            oid: 1.3.6.1.2.1.31.1.1.1.18
            type: DisplayString
          - labels: [ifIndex]
            labelname: ifDescr
            oid: 1.3.6.1.2.1.2.2.1.2
            type: DisplayString
          - labels: [ifIndex]
            labelname: ifName
            oid: 1.3.6.1.2.1.31.1.1.1.1
            type: DisplayString
      - name: ifHCOutOctets
        oid: 1.3.6.1.2.1.31.1.1.1.10
        type: counter
        help: The total number of octets transmitted out of the interface, including framing characters - 1.3.6.1.2.1.31.1.1.1.10
        indexes:
          - labelname: ifIndex
            type: gauge
        lookups:
          - labels: [ifIndex]
            labelname: ifAlias
            oid: 1.3.6.1.2.1.31.1.1.1.18
            type: DisplayString
          - labels: [ifIndex]
            labelname: ifDescr
            oid: 1.3.6.1.2.1.2.2.1.2
            type: DisplayString
          - labels: [ifIndex]
            labelname: ifName
            oid: 1.3.6.1.2.1.31.1.1.1.1
            type: DisplayString

  # HOST-RESOURCES + UCD-SNMP host slice (snmp_exporter v0.26). Any device class.
  # Missing OIDs stay empty — this is not a vendor SKU module.
  host:
    timeout: 8s
    retries: 1
    max_repetitions: 25
    walk:
      - 1.3.6.1.2.1.25.3.3.1.2
      - 1.3.6.1.2.1.25.2.3.1.3
      - 1.3.6.1.2.1.25.2.3.1.4
      - 1.3.6.1.2.1.25.2.3.1.5
      - 1.3.6.1.2.1.25.2.3.1.6
      - 1.3.6.1.4.1.2021.10.1.5
      - 1.3.6.1.4.1.2021.9.1.2
      - 1.3.6.1.4.1.2021.9.1.9
      - 1.3.6.1.4.1.2021.2.1.2
      - 1.3.6.1.4.1.2021.2.1.5
      - 1.3.6.1.4.1.2021.2.1.100
    get:
      - 1.3.6.1.4.1.2021.11.11.0
      - 1.3.6.1.4.1.2021.4.5.0
      - 1.3.6.1.4.1.2021.4.6.0
      - 1.3.6.1.4.1.2021.4.3.0
      - 1.3.6.1.4.1.2021.4.4.0
    metrics:
      - name: hrProcessorLoad
        oid: 1.3.6.1.2.1.25.3.3.1.2
        type: gauge
        help: The average, over the last minute, of the percentage of this processor's load.
        indexes:
          - labelname: hrDeviceIndex
            type: gauge
      - name: hrStorageAllocationUnits
        oid: 1.3.6.1.2.1.25.2.3.1.4
        type: gauge
        help: The size, in bytes, of the data objects allocated from this pool.
        indexes:
          - labelname: hrStorageIndex
            type: gauge
        lookups:
          - labels: [hrStorageIndex]
            labelname: hrStorageDescr
            oid: 1.3.6.1.2.1.25.2.3.1.3
            type: DisplayString
      - name: hrStorageSize
        oid: 1.3.6.1.2.1.25.2.3.1.5
        type: gauge
        help: The size of the storage in hrStorageAllocationUnits.
        indexes:
          - labelname: hrStorageIndex
            type: gauge
        lookups:
          - labels: [hrStorageIndex]
            labelname: hrStorageDescr
            oid: 1.3.6.1.2.1.25.2.3.1.3
            type: DisplayString
      - name: hrStorageUsed
        oid: 1.3.6.1.2.1.25.2.3.1.6
        type: gauge
        help: The amount of the storage used in hrStorageAllocationUnits.
        indexes:
          - labelname: hrStorageIndex
            type: gauge
        lookups:
          - labels: [hrStorageIndex]
            labelname: hrStorageDescr
            oid: 1.3.6.1.2.1.25.2.3.1.3
            type: DisplayString
      - name: ssCpuIdle
        oid: 1.3.6.1.4.1.2021.11.11
        type: gauge
        help: The percentage of time the CPU has spent idle.
      - name: memTotalReal
        oid: 1.3.6.1.4.1.2021.4.5
        type: gauge
        help: Total real/physical memory in kB.
      - name: memAvailReal
        oid: 1.3.6.1.4.1.2021.4.6
        type: gauge
        help: Available real/physical memory in kB.
      - name: memTotalSwap
        oid: 1.3.6.1.4.1.2021.4.3
        type: gauge
        help: Total swap in kB.
      - name: memAvailSwap
        oid: 1.3.6.1.4.1.2021.4.4
        type: gauge
        help: Available swap in kB.
      - name: laLoadInt
        oid: 1.3.6.1.4.1.2021.10.1.5
        type: gauge
        help: Integer load average (load * 100) for 1, 5, and 15 minutes.
        indexes:
          - labelname: laIndex
            type: gauge
      - name: dskPercent
        oid: 1.3.6.1.4.1.2021.9.1.9
        type: gauge
        help: Percentage of space used on disk.
        indexes:
          - labelname: dskIndex
            type: gauge
        lookups:
          - labels: [dskIndex]
            labelname: dskPath
            oid: 1.3.6.1.4.1.2021.9.1.2
            type: DisplayString
      - name: prCount
        oid: 1.3.6.1.4.1.2021.2.1.5
        type: gauge
        help: Number of running instances of a watched process (snmpd proc table).
        indexes:
          - labelname: prIndex
            type: gauge
        lookups:
          - labels: [prIndex]
            labelname: prNames
            oid: 1.3.6.1.4.1.2021.2.1.2
            type: DisplayString
      - name: prErrorFlag
        oid: 1.3.6.1.4.1.2021.2.1.100
        type: gauge
        help: 1 when a watched process is missing or over the configured limit.
        indexes:
          - labelname: prIndex
            type: gauge
        lookups:
          - labels: [prIndex]
            labelname: prNames
            oid: 1.3.6.1.4.1.2021.2.1.2
            type: DisplayString
  # forgesre-onboard-modules begin
  # forgesre-onboard-modules end
