global:
  scrape_interval: 15s
  evaluation_interval: 15s

rule_files:
  - /etc/prometheus/alerts.yml

alerting:
  alertmanagers:
    - static_configs:
        - targets: ["127.0.0.1:9093"]

scrape_configs:
  - job_name: forgesre-core
    scrape_interval: 5s
    metrics_path: /metrics
    static_configs:
      - targets: ["127.0.0.1:__CORE_PORT__"]
        labels:
          asset: forge-demo-01
          instance: forge-demo-01
  - job_name: forgesre-inventory
    metrics_path: /metrics
    http_sd_configs:
      - url: http://127.0.0.1:__CORE_PORT__/api/v1/sd/prometheus
        refresh_interval: 30s
        authorization:
          type: Bearer
          credentials: __WEBHOOK_TOKEN__
  - job_name: forgesre-snmp
    scrape_interval: 30s
    scrape_timeout: 25s
    metrics_path: /snmp
    params:
      module: [if_mib]
      auth: [public_v2]
    http_sd_configs:
      - url: http://127.0.0.1:__CORE_PORT__/api/v1/sd/snmp
        refresh_interval: 30s
        authorization:
          type: Bearer
          credentials: __WEBHOOK_TOKEN__
    relabel_configs:
      - source_labels: [__address__]
        target_label: __param_target
      - source_labels: [snmp_module]
        target_label: __param_module
      - source_labels: [snmp_auth]
        target_label: __param_auth
      - source_labels: [__param_target]
        target_label: instance
      - target_label: __address__
        replacement: 127.0.0.1:9116
