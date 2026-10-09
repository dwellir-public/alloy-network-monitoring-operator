# Manual Metrics Jobs Design

## Summary

Add a `manual-metrics-jobs` config option to `alloy-vm-operator` so operators can define extra scrape jobs for non-related Prometheus targets without replacing the full managed Alloy configuration.

## Decisions

- The operator surface is a charm config option, not a new relation.
- The payload schema is narrow and Alloy-native rather than raw Prometheus YAML.
- Release 1 supports `http` and `https` targets with optional `insecure_skip_verify`.
- Manual jobs inherit local Alloy `juju_*` labels and cannot override them.
- Manual jobs follow the same remote-write gating as relation-derived jobs.

## Shape

Example:

```yaml
- name: external-node-exporter
  targets:
    - "10.20.30.40:9100"
  metrics_path: /metrics
  scheme: http
  scrape_interval: 30s
  scrape_timeout: 10s
  insecure_skip_verify: false
  labels:
    env: prod
    role: node
```

Supported fields:

- `name`
- `targets`
- `metrics_path`
- `scheme`
- `scrape_interval`
- `scrape_timeout`
- `insecure_skip_verify`
- `labels`

Not supported:

- auth
- client certs
- relabeling
- service discovery
- per-target labels

## Runtime Behavior

- Parse manual jobs from charm config.
- Translate them into `MetricsScrapeJob` instances.
- Merge them with relation-derived jobs before rendering Alloy config.
- Enable them only when at least one `send-remote-write` endpoint exists.
- Broaden the waiting status to mention manual and related scraping.
- Reject malformed config with `BlockedStatus` and keep the last good config.
