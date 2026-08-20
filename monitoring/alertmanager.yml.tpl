global:
  resolve_timeout: 5m

route:
  receiver: forgesre
  group_by: ["alertname", "asset"]
  group_wait: 5s
  group_interval: 10s
  repeat_interval: 2h

receivers:
  - name: forgesre
    webhook_configs:
      - url: http://127.0.0.1:__CORE_PORT__/api/v1/webhooks/alertmanager
        send_resolved: true
        http_config:
          authorization:
            type: Bearer
            credentials: __WEBHOOK_TOKEN__
