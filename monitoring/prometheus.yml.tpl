global:
  scrape_interval: 5s
  evaluation_interval: 5s

rule_files:
  - /etc/prometheus/alerts.yml

alerting:
  alertmanagers:
    - static_configs:
        - targets: ["127.0.0.1:9093"]

scrape_configs:
  - job_name: forgesre-core
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
