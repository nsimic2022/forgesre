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
