# alloy-network-monitoring

A Juju machine charm for continuous NetBox discovery, SNMPv3 collection and
metrics forwarding with Grafana Alloy. It retains the existing metrics, logs,
syslog and observability integrations from `alloy-vm`.

Network monitoring is opt-in. `netbox-token` and `snmp-auth` accept granted Juju
secret references. One SNMP secret holds multiple named authentication profiles;
each device selects a profile through its NetBox `snmp_auth` custom field.
`netbox-site`, `netbox-region` and `netbox-tags` are independently optional.

Start with the [network monitoring operator guide](docs/network-monitoring.md),
the [two-profile secret example](examples/snmp-auth.yaml), and the
[build and verification instructions](docs/build-test-deploy.md).
The [implementation record](docs/network-monitoring-implementation.md) distinguishes
local validation from the outstanding live lab acceptance checks.


Supported bases:

- Ubuntu 22.04 amd64
- Ubuntu 24.04 amd64

## Metrics scraping to Mimir

Alloy now supports:

- local self metrics and `prometheus.exporter.unix` host metrics
- relation-driven machine-local workload telemetry over `machine-observability`
- relation-driven scrape targets over `metrics-endpoint` (`prometheus_scrape`)
- manual scrape targets over `manual-metrics-jobs`
- forwarding metrics upstream over `send-remote-write` (`prometheus_remote_write`)

## Machine Observability

`alloy-network-monitoring` can act as a principal machine collector for multiple other
principal charms on the same host.

It consumes:

- endpoint name: `machine-observability`
- interface name: `machine_observability`

This path is intended for shared-machine aggregation where one `alloy-network-monitoring` unit
collects telemetry from multiple colocated principals such as `op-node` and
`op-reth`.

### Contract requirements

`alloy-network-monitoring` accepts v2 and v3 `machine_observability` payloads from related
providers:

- `schema_version: 2` or `schema_version: 3`
- `source_topology`

`source_topology` is required for telemetry because `alloy-network-monitoring` has no
subordinate attachment from which to derive a fallback. In addition, non-empty
v3 rule sets require valid `source_topology.model_uuid` and
`source_topology.application` for stable ownership.

If a related provider still publishes v1 payloads without `source_topology`,
`alloy-network-monitoring` blocks with a message like:

- `machine-observability from <app> requires schema_version 2 or 3 with source_topology`

V3 retains the v2 telemetry shape and adds `prometheus_alert_rules` and
`loki_alert_rules`. Artifacts use `gzip+base64`, include a decoded-byte SHA-256,
and form complete desired state. The full payload may be exactly `60 * 1024`
bytes but no larger, and is not chunked; at most 32 artifacts are admitted per
relation.

Alloy validates bounded rule documents and their PromQL/LogQL with packaged
`cos-tool`, an internal hook-time CLI rather than a service or plugin. It
injects labels from the original payload's source topology exactly once.
Placeholder-bearing expressions and group names are rewritten for topology
injection and deterministic scoping; non-topology labels are preserved. A malformed,
future-version, or structurally invalid outer payload retains the whole
relation's leader-shared LKG. Within a valid v3 payload, a malformed artifact
retains only its own LKG. Valid omission and relation removal withdraw owned
rules while unrelated telemetry continues.

### What gets rendered

For each related principal, `alloy-network-monitoring` translates the payload into:

- per-source `prometheus.scrape` jobs using the provider's topology labels
- per-source `loki.process` blocks with the provider's topology labels
- journald sources for declared `systemd_units`
- journald sources for declared `journal_match_expressions`
- file log sources for declared `log_files`

This means one `alloy-network-monitoring` unit can forward:

- `op-node` metrics and logs labeled as `op-node`
- `op-reth` metrics and logs labeled as `op-reth`
- its own local Alloy and host metrics labeled as `alloy-network-monitoring`

### Example relations

```bash
juju relate alloy-network-monitoring:machine-observability op-node:machine-observability
juju relate alloy-network-monitoring:machine-observability op-reth:machine-observability
juju relate alloy-network-monitoring:send-remote-write mimir-vm:receive-remote-write
juju relate alloy-network-monitoring:send-loki-logs loki-vm:loki_push_api
```

With gateways, add their separate rule-forwarding relations:

```bash
juju relate alloy-network-monitoring:send-loki-logs loki-loadbalancer-vm:loki_push_api
juju relate loki-loadbalancer-vm:loki-alert-rules loki-vm:loki_push_api
juju relate loki-loadbalancer-vm:ingress loki-vm:ingress
juju relate alloy-network-monitoring:send-remote-write mimir-gateway-vm:receive-remote-write
juju relate mimir-gateway-vm:mimir-alert-rules mimir-vm:receive-remote-write
juju relate mimir-gateway-vm:backend mimir-vm:backend
```

Dashboards do not pass through Alloy; each principal relates its standard
`grafana_dashboard` endpoint directly to Grafana VM or Grafana K8s, including
over CMR.

Verify the related payloads on the Alloy unit:

```bash
juju show-unit alloy-network-monitoring/0
```

Verify the rendered config:

```bash
juju ssh alloy-network-monitoring/0 'grep -n "op_node_0" -A20 /etc/alloy/config.alloy'
juju ssh alloy-network-monitoring/0 'grep -n "op_reth_0" -A20 /etc/alloy/config.alloy'
```

## Grafana Cloud Integrator

`alloy-network-monitoring` can also consume Grafana Cloud endpoints and credentials from
`grafana-cloud-integrator` over:

- endpoint name: `grafana-cloud-config`
- interface name: `grafana_cloud_config`

This relation is separate from:

- `send-remote-write` for plain Prometheus remote write URLs
- `send-loki-logs` for plain Loki push URLs

When `grafana-cloud-config` is related, `alloy-network-monitoring` can render authenticated:

- `prometheus.remote_write` endpoints for Grafana Cloud metrics
- `loki.write` endpoints for Grafana Cloud logs

If `send-remote-write` or `send-loki-logs` is also related, Alloy forwards to
both sets of endpoints. This means Grafana Cloud integration is additive rather
than replacing existing upstreams.

### Credential behavior

`alloy-network-monitoring` consumes the relation data published by `grafana-cloud-integrator`.
That contract supports:

- shared legacy `username` and `password`
- signal-specific credentials such as:
  - `prometheus_username` / `prometheus_password`
  - `loki_username` / `loki_password`

Signal-specific credentials are used when present. This is required for Grafana
Cloud setups where Prometheus and Loki use different instance IDs or tokens.

### Connectivity checks

During `update-status`, `alloy-network-monitoring` probes Grafana Cloud endpoints from the
relation and surfaces failures as a blocked status, for example:

- `Grafana Cloud metrics connectivity failed: ...`
- `Grafana Cloud logs connectivity failed: ...`

This only applies to the Grafana Cloud relation path. Plain `send-remote-write`
and `send-loki-logs` relations keep their current behavior.

### Example deployment

Deploy and configure `grafana-cloud-integrator`:

```bash
juju deploy grafana-cloud-integrator
juju config grafana-cloud-integrator prometheus-url="https://prometheus-prod-39-prod-eu-north-0.grafana.net/api/prom/push"
juju config grafana-cloud-integrator loki-url="https://logs-prod-025.grafana.net/loki/api/v1/push"
juju config grafana-cloud-integrator signal-credentials='
prometheus:
  username: "1076854"
  password: "<prometheus-token>"
loki:
  username: "639149"
  password: "<loki-token>"
'
```

Relate it to Alloy:

```bash
juju relate alloy-network-monitoring:grafana-cloud-config grafana-cloud-integrator:grafana-cloud-config
```

Verify the rendered config on the Alloy unit:

```bash
juju ssh alloy-network-monitoring/0 'grep -n "prometheus.remote_write \\\"metrics\\\"" -A20 /etc/alloy/config.alloy'
juju ssh alloy-network-monitoring/0 'grep -n "loki.write \\\"main\\\"" -A20 /etc/alloy/config.alloy'
```

Expected result:

- the Grafana Cloud Prometheus endpoint is rendered with `basic_auth`
- the Grafana Cloud Loki endpoint is rendered with `basic_auth`
- if plain upstream relations also exist, the rendered Alloy config contains
  both the plain and Grafana Cloud endpoints

The intended release-1 operating model is one Alloy unit doing the scraping and forwarding work.
For the shared observability deployment, Alloy forwards into one shared Mimir
metrics store. Tenant-aware relation metadata is not required or published by
`alloy-network-monitoring`; separation is done through metric labels such as Juju topology.

For a validated local build and unit-test workflow for the
`machine-observability` v2/v3 consumer path, see
[docs/build-test-deploy.md](docs/build-test-deploy.md).

### No-upstream behavior

If Alloy has related or manual scrape targets but no `send-remote-write` relation:

- relation-derived and manual scrape jobs are not enabled
- no `prometheus.remote_write` component is rendered
- local self metrics are dropped rather than buffered
- the unit goes to:
  - `waiting: Waiting for remote write before enabling manual or related metrics scraping`

This is deliberate. The charm avoids accumulating local storage pressure when there is nowhere valid to send scraped metrics.

### Remote write outage buffering

When a valid remote write endpoint exists, Alloy renders a bounded WAL buffer on the
`prometheus.remote_write` component:

- `max_keepalive_time = "30m"`

This means:

- short upstream outages are buffered
- longer outages can still lose older samples once the `30m` keepalive window is exceeded

### Example relation to Mimir

Relate Alloy to a deployed shared metrics endpoint such as `mimir-vm` directly:

```bash
juju relate alloy-network-monitoring:send-remote-write mimir-vm:receive-remote-write
```

If `mimir-gateway-vm` is deployed as the stable ingress/load balancer in front
of Mimir, relate Alloy to the gateway instead:

```bash
juju relate alloy-network-monitoring:send-remote-write mimir-gateway-vm:receive-remote-write
```

In both cases, Alloy consumes a plain `prometheus_remote_write` URL contract.
It does not require `tenant-id`, `X-Scope-OrgID`, or tenant-specific path
handling.

Verify the rendered remote write config on the Alloy unit:

```bash
juju ssh alloy-network-monitoring/<unit> 'grep -n "prometheus.remote_write \\\"metrics\\\"" -A8 /etc/alloy/config.alloy'
```

Verify a local unix-exporter metric in Mimir:

```bash
juju ssh mimir-vm/<unit> "curl -fsS 'http://127.0.0.1:9009/prometheus/api/v1/query?query=node_uname_info{juju_application=\"alloy-network-monitoring\"}'"
```

### Add a manual metrics scrape job

You can configure Alloy to scrape extra metrics targets that are not provided
over a Juju relation.

Prerequisite: Alloy only enables manual metrics jobs when it has an upstream
remote-write destination.

Relate Alloy to Mimir first:

```bash
juju relate alloy-network-monitoring:send-remote-write mimir-vm:receive-remote-write
```

Create a YAML file describing the extra scrape job:

```yaml
# manual-metrics-jobs.yaml
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

Apply it to the charm:

```bash
juju config alloy-network-monitoring manual-metrics-jobs="$(cat manual-metrics-jobs.yaml)"
```

What Alloy will do:

- render a dedicated `prometheus.scrape` component for `external-node-exporter`
- inject local Juju topology labels such as `juju_model`, `juju_application`, and `juju_unit`
- forward the scraped samples through the existing `prometheus.remote_write` path

Verify the rendered config on the Alloy unit:

```bash
juju ssh alloy-network-monitoring/0 'grep -n "prometheus.scrape \"external_node_exporter\"" -A20 /etc/alloy/config.alloy'
```

Verify the job label in Mimir:

```bash
juju ssh mimir-vm/0 "curl -fsS 'http://127.0.0.1:9009/prometheus/api/v1/query?query=up{job=\"external-node-exporter\"}'"
```

To remove the manual job again:

```bash
juju config alloy-network-monitoring manual-metrics-jobs=""
```

Notes:

- `name` and `targets` are required.
- `scheme` must be `http` or `https`.
- Release 1 does not support auth, client certificates, relabeling, or service discovery in manual jobs.
- If `send-remote-write` is not related, Alloy will not enable the manual job and the unit will wait for remote write.

## Reconfigure Alloy with full override

Dump the current override value:

```bash
juju config alloy-network-monitoring config-override
```

Set a full config override from a file:

```bash
juju config alloy-network-monitoring config-override="$(cat alloy.conf)"
```

Clear override and return to charm-generated config:

```bash
juju config alloy-network-monitoring config-override=""
```

## Enable live debugging

Enable live debugging mode:

```bash
juju config alloy-network-monitoring alloy-livedebugging=true
```

What this does:
- Sets `CUSTOM_ARGS` to `--server.http.listen-addr=0.0.0.0:12345` in `/etc/default/alloy`.
- Adds a `livedebugging { enabled = true }` block to generated Alloy config.

Verify on the unit:

```bash
juju ssh alloy-network-monitoring/<unit> 'grep "^CUSTOM_ARGS=" /etc/default/alloy'
juju ssh alloy-network-monitoring/<unit> 'grep -n "livedebugging" /etc/alloy/config.alloy'
```

Disable live debugging and restore previous args:

```bash
juju config alloy-network-monitoring alloy-livedebugging=false
```

Optional: set your baseline custom args that should be restored after disabling:

```bash
juju config alloy-network-monitoring custom_args="--server.http.listen-addr=0.0.0.0:6987"
```

## Enable syslog receivers (phase 6)

Enable TCP+UDP syslog listeners on port `1514`:

```bash
juju config alloy-network-monitoring enable-syslogreceivers=true
```

Behavior:
- Starts `loki.source.syslog` listeners for both TCP and UDP on `:1514`.
- Applies relabel rules for remote syslog metadata.
- Routes remote syslog through a dedicated `loki.process "remote_syslog"` stage before
  `loki.write.main`.
- Does not add Juju topology labels to remote syslog.
- If there is no `send-loki-logs` relation, syslog is dropped (`forward_to = []`).

### Optional remote syslog drop controls

The remote syslog pipeline can be reduced before forwarding to Loki.

Charm config:

- `syslog-drop-access-logs`
- `syslog-drop-expressions`
- `syslog-rate-limit`
- `syslog-rate-burst`

Behavior:

- `syslog-drop-access-logs=true` drops common request-style access-log lines such as
  `GET ... HTTP/...` before they reach Loki.
- `syslog-drop-expressions` accepts newline-separated regular expressions for additional
  remote-syslog-only drops.
- `syslog-rate-limit` and `syslog-rate-burst` apply a `stage.limit` guard to the
  surviving remote syslog stream.

This filtering only affects logs received through `loki.source.syslog "receiver"`.
Local journal scraping is unaffected.

Recommended operator use:

- start by suppressing noisy logs at the source charm when possible
- use Alloy as the second guardrail when a related charm still emits too much remote syslog
- keep Loki retention finite as the final guardrail

Recommended starting profiles:

- keep all remote syslog:
  - leave all `syslog-*` drop controls at their defaults
- drop common HAProxy-style access logs but keep service/application logs:
  - `syslog-drop-access-logs=true`
- drop known noisy custom patterns as well:
  - set `syslog-drop-expressions` to newline-separated regular expressions
- rate-limit any surviving bursty remote syslog:
  - set both `syslog-rate-limit` and `syslog-rate-burst`

Example: drop common access logs

```bash
juju config alloy-network-monitoring syslog-drop-access-logs=true
```

Example: drop access logs and add a burst guard

```bash
juju config alloy-network-monitoring syslog-rate-limit=25
juju config alloy-network-monitoring syslog-rate-burst=100
```

Example: add custom drop expressions

```bash
juju config alloy-network-monitoring syslog-drop-expressions=$'healthcheck\\n/readiness\\n/livez'
```

This means:

- request-style access logs are removed before they reach Loki
- only `25` surviving remote-syslog lines per second are kept
- short bursts up to `100` lines are allowed before excess lines are dropped

This is useful when a related charm can generate enough access traffic to
inflate Loki ingestion and object-storage growth.

This is intended as a second guardrail. Prefer suppressing noisy logs at the source
charm first when possible.

Reset to default behavior:

```bash
juju config alloy-network-monitoring syslog-drop-access-logs=false
juju config alloy-network-monitoring syslog-drop-expressions=''
juju config alloy-network-monitoring syslog-rate-limit=0
juju config alloy-network-monitoring syslog-rate-burst=0
```

Verify on the unit:

```bash
juju ssh alloy-network-monitoring/<unit> 'grep -n "loki.process \"remote_syslog\"" -A20 /etc/alloy/config.alloy'
```

Verify in Loki/Grafana:

- send a known manual syslog message and confirm it still appears
- trigger the noisy traffic pattern, for example HAProxy access requests
- confirm the access-log lines no longer appear while the manual message still does

### `syslog-receiver` relation contract

When another charm relates to `alloy-network-monitoring:syslog-receiver`, Alloy now publishes the
receiver details over relation data.

Published keys:
- `address`
- `port`
- `protocols`
- `recommended-protocol`
- `ready`
- `reason`

Release-1 behavior:
- `port` is `1514`
- `protocols` is `tcp,udp`
- `recommended-protocol` is `tcp`
- `ready=true` only when syslog receivers are enabled and Alloy has an active
  `send-loki-logs` path to Loki
- if Alloy would drop the received logs because no Loki relation exists, it
  publishes `ready=false` and `reason=waiting for send-loki-logs relation`

This makes Alloy suitable as the immediate syslog receiver for charms such as
`haproxy-dataplane-api`, while `loki-vm` remains the downstream log store.

Verify on the unit:

```bash
juju ssh alloy-network-monitoring/<unit> 'grep -n "loki.source.syslog \\\"receiver\\\"" /etc/alloy/config.alloy'
juju ssh alloy-network-monitoring/<unit> 'grep -n "protocol = \\\"udp\\\"\\|protocol = \\\"tcp\\\"" /etc/alloy/config.alloy'
```

Disable syslog listeners:

```bash
juju config alloy-network-monitoring enable-syslogreceivers=false
```

## Capture host kernel and raw journal matches

The default host log path in `alloy-network-monitoring` only follows the named systemd units
from `systemd-units`. You can extend this with:

- `journal-kernel`
- `journal-match-expressions`

These options create an additional host journal source in Alloy.

Behavior:

- `systemd-units` logs still flow through the Juju-labeled log pipeline
- kernel/raw host journal logs do not get `juju_*` labels
- if there is no `send-loki-logs` relation, the broader host journal path is
  dropped just like the existing log pipeline

### `journal-kernel`

Enable host kernel transport logs:

```bash
juju config alloy-network-monitoring journal-kernel=true
```

What happens:

- Alloy adds `_TRANSPORT=kernel` to a broader host journal source
- this is intended to capture kernel log traffic similar to `dmesg`
- these entries are forwarded to Loki without Juju topology labels

### `journal-match-expressions`

Add raw journald match expressions as newline-separated clauses:

```bash
juju config alloy-network-monitoring journal-match-expressions=$'SYSLOG_IDENTIFIER=lxd'
```

What happens:

- the expressions are passed through without validation
- empty lines are ignored
- invalid expressions can break the rendered Alloy config and cause config
  validation to fail

Example: capture both kernel transport logs and host LXD messages

```bash
juju config alloy-network-monitoring journal-match-expressions=$'_TRANSPORT=kernel\nSYSLOG_IDENTIFIER=lxd'
```

Example: keep unit-based logs and add kernel capture

```bash
juju config alloy-network-monitoring systemd-units='ssh.service,snap.lxd.daemon.service'
juju config alloy-network-monitoring journal-kernel=true
```

What happens:

- `ssh.service` and `snap.lxd.daemon.service` logs still keep Juju topology labels
- kernel transport logs are collected through the separate host journal path
- the host journal path remains unlabeled by Juju topology

Example: monitor an LXD host for kernel, LXD, and audit-style host messages

```bash
juju config alloy-network-monitoring systemd-units='snap.lxd.daemon.service'
juju config alloy-network-monitoring journal-kernel=true
juju config alloy-network-monitoring journal-match-expressions=$'SYSLOG_IDENTIFIER=audit\nSYSLOG_IDENTIFIER=lxd'
```

What happens:

- `snap.lxd.daemon.service` logs are collected through the Juju-labeled service path
- kernel transport logs are collected through the broader host journal path
- host messages emitted with `SYSLOG_IDENTIFIER=lxd` or `SYSLOG_IDENTIFIER=audit`
  are collected through the broader host journal path
- the broader host journal path does not add `juju_*` labels, so these entries
  should be treated as host-scoped logs rather than Juju workload-scoped logs

### Verification

Inspect the rendered host journal blocks:

```bash
juju ssh alloy-network-monitoring/<unit> 'grep -n "loki.source.journal" -A6 /etc/alloy/config.alloy'
```

Check that `_TRANSPORT=kernel` or your raw match expression is present:

```bash
juju ssh alloy-network-monitoring/<unit> 'grep -n "_TRANSPORT=kernel\\|SYSLOG_IDENTIFIER=lxd" /etc/alloy/config.alloy'
```

Query recent logs in Loki or Grafana after triggering a known matching message:

```bash
juju ssh loki-vm/<unit> 'start=$(date -u -d "15 minutes ago" +%s%N); end=$(date -u +%s%N); curl -sG --data-urlencode "query={}" --data-urlencode limit=20 --data-urlencode start=$start --data-urlencode end=$end http://127.0.0.1:3100/loki/api/v1/query_range'
```

If you added a raw identifier match, trigger the matching host service and look for
the expected log line content in Grafana Explore or through Loki's API.

```bash
juju ssh alloy-network-monitoring/<unit> 'journalctl -n 20 --no-pager -u snap.lxd.daemon.service'
```


## Alert rule transport

Alert rules use the receiver's `alert_rules_encodings` advertisement to negotiate
Canonical-compatible LZMA/base64 encoding in the existing `alert_rules` field.
Legacy receivers continue to receive JSON. The publisher uses Canonical's public
`cosl.LZMABase64` codec when the receiver advertises LZMA support. Publishing keeps
an 8 MiB decoded budget and a 60 KiB relation-value ceiling; oversized output is
reported rather than truncated. Artifact validation and last-known-good handling
remain local to this charm.

Upgrade Loki/Mimir receivers and gateways before collectors when aggregates exceed
the JSON size limit. Encoded values remain below 60 KiB, with the collector's
existing stricter publication guard retained. Compression does not change source
topology, machine-observability schema/artifact encoding, or artifact-count limits.
