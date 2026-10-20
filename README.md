# alloy-nm

A Juju machine charm that discovers devices in NetBox, collects SNMPv2c/v3 metrics
with Grafana Alloy, and forwards metrics to Mimir and logs to Loki. It also keeps
the original `alloy-vm` features: host metrics, manual scrape jobs, related
workload metrics and logs, journald, syslog reception and Grafana Cloud forwarding.

Supported machines: **Ubuntu 22.04 or 24.04, amd64**. Fresh deployments install
Alloy **1.20.1-1**. Run one collector unit per application; choose different
inventory scopes for additional applications. Coordinated HA is not implemented.

- [Get started](#get-started)
- [Choose SNMP modules for each device](#choose-snmp-modules-for-each-device)
- [Receive and parse device logs](#receive-and-parse-device-logs)
- [Enable and tune alerts](#enable-and-tune-alerts)
- [Scrape an existing node-exporter](#scrape-an-existing-node-exporter)
- [All Juju configuration options](#all-juju-configuration-options)
- [Existing integrations](#existing-integrations)
- [Troubleshooting and lifecycle](#troubleshooting-and-lifecycle)

## Get started

### 1. Connect the destinations

Build the charm using the [build instructions](docs/build-test-deploy.md), then
deploy the artifact for your Ubuntu base. Substitute your existing backend
application names in the relations:

```bash
juju deploy ./alloy-nm_ubuntu@24.04-amd64.charm --base ubuntu@24.04
juju relate alloy-nm:send-remote-write mimir-vm:receive-remote-write
juju relate alloy-nm:send-loki-logs loki-vm:loki_push_api
```

Metrics need a remote-write destination, supplied by `send-remote-write` or
`grafana-cloud-config`. Without one, the charm waits before enabling network,
manual or related metrics scraping. Logs need a Loki destination.

### 2. Create and grant the secrets

Copy the [SNMP authentication template](examples/example.snmp_auth.yaml) outside
this checkout and replace its placeholders with the device credentials. Save
your NetBox token in a separate file. The token file contains **only the raw
token**, without `Bearer` or `Token`.

For example, a file named `netbox-token.txt` contains one line (dummy value):

```text
0123456789abcdef0123456789abcdef01234567
```

Replace that dummy value with the complete token copied from NetBox. The file has no
`token:` key, quotes or authorization prefix. Juju's `token#file` syntax supplies
the secret field name; the charm adds the configured header prefix.

```bash
juju add-secret netbox-token 'token#file=./netbox-token.txt'
juju add-secret snmp-auth 'snmp-yaml#file=./snmp_auth.yaml'

# Use the secret references returned by the commands above.
juju grant-secret secret:NETBOX-ID alloy-nm
juju grant-secret secret:SNMP-ID alloy-nm
```

`netbox-token` and `snmp-auth` are Juju secret references, never passwords in Juju
config. The SNMP secret must use the **`snmp-yaml`** field, not `token`. One secret
supports up to 128 named profiles; a device's NetBox `snmp_auth` custom field
selects its profile, for example `opnsense` or `cisco`.

The SNMP YAML contains only an `auths` mapping. **SNMPv2c and SNMPv3 profiles can
share the same secret.** Each profile chooses its protocol using an explicit
integer `version`.

For SNMPv2c, add a profile to your existing `auths` mapping, retaining the other
profiles:

```yaml
auths:
  legacy_v2c:
    version: 2
    community: "REPLACE-WITH-READ-ONLY-COMMUNITY"
  # Keep your existing SNMPv3 profiles here too.
```

Set the device's NetBox `snmp_auth` field to `legacy_v2c`, then update the same
secret:

```bash
juju update-secret secret:SNMP-ID 'snmp-yaml#file=./snmp_auth.yaml'
```

A v2c profile accepts only `version: 2` and a nonempty string `community`.
Quote community values, especially if they look like numbers. No username,
authentication/privacy protocol or password fields are used. The community is
stored through Juju secrets and protected local files, using the same lifecycle
as v3 credentials. SNMPv2c itself provides no encryption on the device connection.

SNMPv3 profiles accept:

| Field | Meaning |
| --- | --- |
| `version` | Required integer `3`. |
| `username` | Required SNMPv3 username. |
| `security_level` | Required: `noAuthNoPriv`, `authNoPriv` or `authPriv`. |
| `auth_protocol` | Required for authenticated modes: `MD5`, `SHA`, `SHA224`, `SHA256`, `SHA384` or `SHA512`. |
| `password` | Authentication passphrase, at least eight characters when required. |
| `priv_protocol` | Required for `authPriv`: `DES`, `AES`, `AES192`, `AES256`, `AES192C` or `AES256C`. |
| `priv_password` | Privacy passphrase, at least eight characters for `authPriv`. |
| `context_name` | Optional SNMP context. |

Algorithms and credentials must match the device. The secret is limited to
1 MiB. Module definitions and SNMPv1 profiles are rejected. Missing/invalid
versions, empty communities and mixed v2c/v3 fields are rejected rather than
falling back to a default community.

**Most installations can leave `netbox-ca` empty.** HTTPS certificates issued by
a CA already trusted by the collector use Ubuntu's normal certificate trust.
An extra secret is needed only when the collector does not trust the certificate
chain, such as a private CA or self-signed deployment. A privately issued
certificate may need this even when its leaf certificate is not self-signed.
Certificate verification stays enabled.

For those deployments, create and grant the optional CA secret:

```bash
juju add-secret netbox-ca 'ca-pem#file=./netbox-ca.pem'
juju grant-secret secret:CA-ID alloy-nm
juju config alloy-nm netbox-ca=secret:CA-ID
```

### 3. Enable discovery

Your NetBox installation needs
[netbox-plugin-prometheus-sd](https://github.com/FlxPeters/netbox-plugin-prometheus-sd),
enabled as `netbox_prometheus_sd` in NetBox. Follow the plugin's installation and
version-compatibility instructions. It exposes
`/api/plugins/prometheus-sd/devices/`, which the charm reads automatically; set
`netbox-url` to the NetBox base URL. Devices need a primary IPv4 address and a
`snmp_auth` profile name found in the secret.

Replace `<...>` placeholders in the following examples with your own values:

```bash
juju config alloy-nm \
  network-monitoring-enabled=true \
  collector-id='<collector-name>' \
  netbox-url=https://netbox.example \
  netbox-token=secret:NETBOX-ID \
  snmp-auth=secret:SNMP-ID
```

`collector-id` is a name you choose for this collector's monitoring scope. It is
required when network monitoring is enabled. It does not have to match the Juju
application or a NetBox object. For example, two applications covering different
sites could use `edge-collector-01` and `edge-collector-02`.

The value appears as `collector="<collector-name>"` on SNMP metrics, the
collector's self/host metrics and its metric alert rules. It lets dashboards and
alerts select the right collection scope even when several collectors send to
one Mimir instance. Juju labels still identify the model/application/unit, and
NetBox labels identify the device.

Choose a unique name across collectors sharing a backend and keep it stable.
Changing it creates new metric series and changes alert selectors; existing
history keeps the previous label. It does not select a site, filter devices,
coordinate HA or prevent overlapping collectors from scraping the same device.
Use letters, numbers, dots, dashes or underscores, with at most 64 characters.

Site, region and tags are **independently optional**:

```bash
juju config alloy-nm \
  netbox-site='<site-name>' \
  netbox-region='<region-name>' \
  netbox-tags='<tag-name>,<another-tag-name>'
```

An empty filter selects everything for that dimension. Multiple tags require
**all** listed tags. Status defaults to `active`; use `netbox-status=""` to
include every status. The region filter is handled by NetBox; descendant-region
behavior depends on the installed NetBox version.
For site, region and tag placeholders, use the corresponding NetBox **slugs**,
which can differ from their display names.

Configured `netbox-site` and `netbox-region` also add default **`site`** and
**`region`** labels to all metrics and logs forwarded by the generated
configuration. This includes SNMP, manual scrape jobs, host/self metrics,
journal/file logs and received syslog. Existing source labels take precedence;
for example, SNMP keeps its discovered site. An empty option adds no label.
These defaults also work with `network-monitoring-enabled=false`, so a
syslog-only application needs no NetBox credentials to use them.

The configured region describes the collection scope, even when NetBox includes
descendant regions; it does not claim each device belongs directly to that
region. Setting, changing or clearing the defaults creates new metric series
and log streams; historical data keeps its existing labels. For example:

```promql
up{job="snmp/network", site="<site-name>", region="<region-name>"}
```

```logql
{log_source="remote_syslog", site="<site-name>", region="<region-name>"}
```

Managed SNMP targets also carry the device's NetBox **role slug** as `role`,
such as `edge-router`. This uses the per-device `__meta_netbox_role_slug`
discovery metadata and needs no extra Juju configuration. Devices with no role
slug still collect normally, without a `role` label. Role changes appear after
the next discovery refresh; historical samples keep their previous labels.

```promql
up{job="snmp/network", role="edge-router"}
```

The default authentication scheme is `Bearer` for NetBox v2 tokens; use
`netbox-auth-scheme=Token` for v1 tokens. The charm adds this prefix itself.

## Choose SNMP modules for each device

The default is `system,if_mib`, providing device uptime and interface metrics.
Change the default for devices without an override:

```bash
juju config alloy-nm snmp-modules=system,if_mib
```

Add an optional NetBox **text** custom field named **`snmp_modules`** to devices.
Use a comma-separated list with **no spaces**:

| Device | `snmp_auth` | `snmp_modules` |
| --- | --- | --- |
| OPNsense router | `opnsense` | `system,if_mib` |
| Cisco Catalyst switch | `cisco` | `system,if_mib,cisco_device` |
| Device using the collector default | A valid profile | Empty or absent |

The per-device list replaces the global list; include `system` and `if_mib` when
you still want their metrics. Changes take effect on the next discovery refresh
without restarting Alloy. Missing, empty, `None` or `null` values use the default.

Each list permits one to eight embedded modules. With managed network monitoring
enabled, the Juju **`snmp-modules`** option is checked before applying
configuration or reloading Alloy. An unknown
name or malformed list produces a Juju **blocked** status and retains the
previous configuration.

The NetBox **`snmp_modules`** custom field for eligible devices is checked during
Alloy's discovery refresh, before device polling. These inventory changes do not reload Alloy.
An invalid list retains a failed target with
`snmp_config_error="invalid_modules"`; valid devices continue collecting.
At the next Juju `update-status` check, the unit reports **blocked** with affected
NetBox device IDs. Correct the field and the status recovers on the next check.
If the local Alloy API cannot be inspected, Juju reports **waiting** while
collection continues.

Validation checks that the names exist in this charm's embedded module catalog.
Whether a device returns the requested OIDs is checked by the actual SNMP scrape.

**A Catalyst 4500-X is one example**, not a restriction on the supported devices.
For that example, `system,if_mib,cisco_device` can be a starting module selection.
Returned OIDs depend on the device's IOS XE release, SNMP views and standalone/VSS
configuration.

`cisco_device` is a module bundling selected OIDs/metrics from several MIBs,
including Cisco CPU, memory pools, FRU power and ENTITY sensors. It is not one MIB
or an instruction to poll every object in those MIBs. The
[upstream generator definition](https://github.com/prometheus/snmp_exporter/blob/v0.30.0/generator/generator.yml)
shows how these collection bundles are assembled.

You can also select individual supported modules such as `if_mib` or `ip_mib`,
for example `system,if_mib,ip_mib`. These names are lowercase; always use the
catalog's **exact, case-sensitive** name. Some entries, such as `hrSystem`, use
mixed case. Lowercasing a raw MIB name does not create a supported module. Custom
MIB/module uploads are not currently included.

The catalog is pinned to **Alloy 1.20.1** and comes from its
[embedded SNMP configuration](https://github.com/grafana/alloy/blob/v1.20.1/internal/static/integrations/snmp_exporter/common/snmp.yml.gz).
The file is gzip-compressed YAML; its `modules` keys are the available names.
Compare the [current upstream catalog](https://github.com/grafana/alloy/blob/main/internal/static/integrations/snmp_exporter/common/snmp.yml.gz)
or switch to a newer release tag to check future additions. Using new entries
requires a compatible Alloy release and an updated charm allowlist. Available
names for this charm are:

```text
apcups arista_sw cisco_device cisco_fc_fe cisco_imc cisco_wlc cyberpower
 dell dell_network dlink eap eltex_mes fieldserver hpe hrDevice hrSWInstalled
 hrSWRun hrSWRunPerf hrStorage hrSystem if_mib infrapower_pdu ip_mib juniper
 juniper_optics keepalived kemp_loadmaster liebert_pdu mikrotik nec_ix
 paloalto_fw powercom printer_mib raritan readynas servertech_sentry3
 servertech_sentry4 sophos_xg synology system tplink-ddm ubiquiti_airfiber
 ubiquiti_airmax ubiquiti_airos ubiquiti_unifi ucd_la_table ucd_memory
 ucd_system_stats yamaha_rt
```

## Receive and parse device logs

Enable the existing TCP and UDP listeners on port **1514**, then select parsers:

```bash
juju config alloy-nm \
  enable-syslogreceivers=true \
  syslog-parsers=opnsense_filterlog,cisco_ios
```

Point the devices' remote syslog settings at the Alloy machine. Port 1514 accepts
RFC5424 syslog. The `syslog-receiver` integration continues to advertise this port.

For legacy devices such as Cisco switches sending **RFC3164** (BSD syslog), enable
the separate receiver on TCP and UDP port **1513**:

```bash
juju config alloy-nm enable-syslog-rfc3164=true syslog-parsers=cisco_ios
```

Configure the switch's syslog destination as the Alloy machine's IP and port 1513.
You can enable either receiver independently, or run both at once. To choose
another RFC3164 port, set `syslog-rfc3164-port=2513`, for example. Ports must be
between 1 and 65535 and must not conflict with the enabled RFC5424 or Alloy HTTP
listener. Invalid settings leave the working configuration in place and report
a blocked Juju status.

Both formats use the same Loki destination, Juju topology/location labels,
source hostname/IP labels, optional parsers, drop rules and rate limit. The rate
limit is shared across both receivers. Connect `send-loki-logs` before forwarding
device logs; without a Loki destination, incoming logs are discarded. Allow the
selected TCP/UDP port through any network firewall. RFC3164 connection details
are configured directly on the sender; the existing integration advertises only
RFC5424. Both formats use reception time for the Loki timestamp.

The new listener accepts standard RFC3164. Cisco IOS may add non-standard header
fields such as sequence counters or fractional timestamps; configure the sender
to produce compatible headers. Alloy's experimental Cisco header extensions
are separate from the `cisco_ios` body parser and are not enabled by this option.
See the [pinned Alloy 1.20.1 syslog reference](https://github.com/grafana/alloy/blob/v1.20.1/docs/sources/reference/components/loki/loki.source.syslog.md).

Parser selection is configurable:

| Profile | What it extracts |
| --- | --- |
| `opnsense_filterlog` | IPv4/IPv6 `filterlog` CSV events: rule, interface, reason, action, direction, protocol, addresses and TCP/UDP ports. |
| `cisco_ios` | `%FACILITY-SEVERITY-MNEMONIC: message` events, classified as interface state/flap, login failure, configuration change, selected hardware alarm or generic message. |

Cisco interface extraction covers LINK and LINEPROTO messages, including the
`Line protocol on Interface` form shown in
[Cisco's interface guide](https://www.cisco.com/c/en/us/td/docs/ios-xml/ios/interface/configuration/15-mt/ir-15-mt-book/ir-cfg-lan-if.html).

OPNsense parsing requires the syslog application name `filterlog`. Cisco parsing
matches the message format. Matching events gain **structured metadata** such as
`event_vendor`, `event_type`, `event_interface`, `event_action`,
`event_src_ip`, `event_dst_ip` and `event_mnemonic`. Ports are extracted only for
TCP/UDP. Unmatched messages are forwarded normally.

The original message is preserved. Parsed fields add no indexed labels, keeping
the existing Loki label budget. Your Loki backend must accept structured
metadata. For example:

```logql
{log_source="remote_syslog"} | event_vendor="opnsense" | event_action="block"
{log_source="remote_syslog"} | event_vendor="cisco" | event_type="interface_state"
```

When both location defaults are set, syslog transport `protocol` moves to
structured metadata to keep streams within Loki's default 15-label limit. Query
it with `{log_source="remote_syslog"} | protocol="udp"`. Device identity remains
available as `syslog_hostname` and `source_ip`; the charm does not infer a NetBox
`device` label for logs.

Set `syslog-parsers=""` to disable body parsing while continuing to receive logs.
Set `enable-syslogreceivers=false` to disable RFC5424 reception, and
`enable-syslog-rfc3164=false` to disable RFC3164. Existing drop expressions
and rate limits apply after parsing and affect which events reach Loki and its
alerts.

## Enable and tune alerts

### Start with the basic defaults

With network monitoring enabled and `send-remote-write` related to a rule-capable
backend, leave
`network-alerts-config` empty to use the basic collector rules. They ship by
default because `network-alerts-enabled=true`. Device/interface alerts, log
alerts and syslog body parsing are opt-in.

| Default collector alert | Condition | Must persist for |
| --- | --- | --- |
| `SNMPDeviceDown` | A discovered device's SNMP scrape has `up=0`. | `5m` |
| `SNMPNoEligibleTargets` | No SNMP target series exists for this collector. | `5m` |
| `NetBoxDiscoveryFailing` | Discovery failures increased during the last `5m`. | `2m` |
| `NetworkCollectorDown` | Collector self-scrape is missing or has `up=0`. | `5m` |
| `NetworkRemoteWriteBacklog` | More than 10,000 samples are pending remote write. | `10m` |
| `NetworkCollectorDiskLow` | A monitored non-tmpfs filesystem has less than 10% free space. | `10m` |

These collector thresholds are fixed. If you enable device rules, the basic
defaults are Cisco CPU/memory above 90% for `10m`, selected interface utilisation above 85%
for `10m`, and an expected administratively up port down for `5m`. Interface
rules also require an explicit selection below; they do not alert on every port.

### How the rules are shipped

The **charm leader** generates the rule groups and publishes them in application
relation data under `alert_rules`, along with collector metadata. It uses JSON
or the existing negotiated compression. This is separate from Alloy sending
metric samples and log entries.

| Rules | Juju relation used | Evaluation happens in |
| --- | --- | --- |
| PromQL metric rules | `send-remote-write` → `receive-remote-write` | Mimir's ruler |
| LogQL event rules | `send-loki-logs` → `loki_push_api` | Loki's ruler |

For direct backend connections:

```bash
juju relate alloy-nm:send-remote-write mimir-vm:receive-remote-write
juju relate alloy-nm:send-loki-logs loki-vm:loki_push_api
```

The receiving charm must consume and install the rules, with its ruler enabled
and routed to Alertmanager for notifications. Relating the applications ships
definitions; enabling an alert family does not configure notification receivers.
For gateways, also connect their backend rule-forwarding relations shown under
[existing integrations](#existing-integrations). A Grafana Cloud endpoint relation
alone does not transport these rules.

Inspect the **receiving unit's** relation data (for example,
`juju show-unit mimir-vm/0` or `juju show-unit loki-vm/0`) for the collector's
`alert_rules`/`metadata` fields. With a gateway, inspect its receiving unit too.
Then check loaded groups and evaluation state in the Mimir/Loki ruler or Grafana
alerting view. Metric/log arrival alone does not prove rule installation.

### Enable additional alert families

| Control | Default | Effect |
| --- | --- | --- |
| `network-alerts-enabled` | `true` | Ships the six existing collector metric alerts. Turning it off also disables new device/interface metric rules. |
| `network-device-alerts-enabled` | `false` | Adds device health and selected interface rules when metric shipping is enabled. |
| `network-log-alerts-enabled` | `false` | Ships event rules for enabled syslog parsers; independent of metric shipping. |

Disabling these flags withdraws the charm's corresponding rule groups. Metrics
and logs keep flowing. Related applications' alert rules continue to be
forwarded using their own source identity.

Enable the new rule families:

```bash
juju config alloy-nm \
  network-device-alerts-enabled=true \
  network-log-alerts-enabled=true
```

Device metric alerts require network monitoring. Event rules require syslog
reception and a matching parser; they can work without NetBox discovery.

The original collector rules are `SNMPDeviceDown`, `SNMPNoEligibleTargets`,
`NetBoxDiscoveryFailing`, `NetworkCollectorDown`, `NetworkRemoteWriteBacklog` and
`NetworkCollectorDiskLow`. New device rules cover invalid module selection,
Cisco CPU/memory, power and sensor faults, and SNMP agent uptime resets.
Unsupported or absent device metrics do not trigger those health rules.

### Select expected interfaces

Interface alerts require an **explicit scope**. Empty configuration enables no
interface alerts, avoiding alarms on unused ports. Copy and edit the
[alert configuration example](examples/example.network-alerts.yaml):

```yaml
interfaces:
  - name: router-wan
    devices: '<router-name>'
    include: 'igb0'
  - name: switch-uplinks
    devices: 'core-.*'
    include: 'TenGigabitEthernet1/(1|2)'
    exclude: '.*\.100'
utilisation:
  threshold: 85
  for: 10m
errors:
  threshold: 1
  min_packets: 10
  for: 10m
logs:
  firewall_blocks:
    enabled: false
```

Apply this YAML as a Juju **string** option:

```bash
juju config alloy-nm network-alerts-config="$(cat ./network-alerts.yaml)"
```

Scope fields:

| Field | Required/default | Meaning |
| --- | --- | --- |
| `name` | Required | Unique scope identifier: 1–32 letters, digits, `_` or `-`; added as `network_alert_profile` on alerts. |
| `devices` | Default `.+` | Regex matching the NetBox device name (`device` label). |
| `include` | Required | Regex matching the SNMP `ifName`. |
| `exclude` | Default empty | Regex excluding `ifName` values. |

Selectors match the complete label value. Use `.*` when you want a partial
match. Up to eight scopes are accepted. Keep scopes disjoint to avoid duplicate
alerts for one interface. Regexes are limited to 128 characters and do not
accept lookarounds, backreferences or possessive repetition.

### Metric preset reference

`network-alerts-config` accepts these top-level preset mappings. Omitted fields
use the defaults below; set `enabled: false` to omit an individual preset. All
presets default to enabled **within the opt-in device family**.

| Preset | Default fields | Behavior |
| --- | --- | --- |
| `interface_down` | `enabled: true`, `for: 5m` | Selected port is operationally down while administratively up. |
| `utilisation` | `enabled: true`, `threshold: 85`, `for: 10m` | Receive and transmit utilisation are checked separately; uses HC octets and positive `ifHighSpeed` in Mb/s. |
| `errors` | `enabled: true`, `threshold: 1`, `min_packets: 10`, `for: 10m` | Error percentage relative to HC unicast/multicast/broadcast packet rate, separately in/out. |
| `discards` | `enabled: true`, `threshold: 1`, `min_packets: 10`, `for: 10m` | Discard percentage with the same packet-rate floor. |
| `cpu` | `enabled: true`, `threshold: 90`, `for: 10m` | Cisco one-minute CPU percentage. |
| `memory` | `enabled: true`, `threshold: 90`, `for: 10m` | Cisco pool usage percentage; enhanced pools preferred over legacy pools on each device. |
| `power` | `enabled: true`, `for: 2m` | Selected fault states from Cisco FRU power state sets; administratively off/spare states are excluded. |
| `sensors` | `enabled: true`, `for: 5m` | ENTITY sensor status is nonoperational; an unavailable sensor alone does not alarm. |
| `uptime_reset` | `enabled: true`, `for: 0s` | Uptime decreases in a 15-minute window, suppressing values near a TimeTicks wrap. This can mean an agent restart, not necessarily a device reboot. |

Percentages must be greater than zero and at most 100. `min_packets` is packets
per second, not a packet count. Interface rates use five-minute windows. `for`
is how long the condition must persist before firing; `0s` means no pending
period. Durations use ordered units such as `5m` or `1h30m` and cannot exceed 24h.
Temperature value alerts are not included: sensor units, scale and precision
need normalisation first.

`SNMPModuleSelectionInvalid` uses a fixed two-minute pending period and is
included whenever device alerts are enabled.

### Event preset reference

Under the top-level `logs` mapping, every preset accepts `enabled`, `count`,
`window` and `for`:

| Preset | Required parser | Defaults | Behavior |
| --- | --- | --- | --- |
| `link_changes` | `cisco_ios` | Enabled; 4 events / `5m`; `for: 1m` | Interface state changes or flaps. |
| `authentication_failures` | `cisco_ios` | Enabled; 5 events / `5m`; `for: 1m` | Login failure messages. |
| `hardware_faults` | `cisco_ios` | Enabled; 1 event / `5m`; `for: 1m` | Selected ENVMON/FAN/POWER/PLATFORM_ENV messages with severity 0–3. |
| `configuration_changes` | `cisco_ios` | Disabled; 1 event / `5m`; `for: 0s` | SYS CONFIG_I messages; opt in if every configuration change should alert. |
| `firewall_blocks` | `opnsense_filterlog` | Disabled; 1,000 events / `5m`; `for: 5m` | Blocked packets; opt in and tune to your normal firewall traffic. |

Counts are positive integers up to 1,000,000. Windows must be positive and at
most 24h. Counts are grouped by sender hostname/instance and parsed interface
where present. Firewall blocks on different interfaces have separate counts.
Routine configuration messages and firewall blocks remain searchable even when
their alert presets are disabled.

The full YAML is limited to 16 KiB. Unknown fields, duplicate keys, invalid
selectors and invalid thresholds block the candidate and retain the previously
applied collection configuration and rules. Existing collector thresholds are
fixed; the new preset YAML tunes device/interface and event rules.

Published rules share the existing 60 KiB relation budget with related providers.
Large interface selections can exceed it even within the eight-scope schema
limit. The charm rejects an oversized candidate while keeping the applied rules;
reduce scopes or disable unneeded presets if it reports a capacity error.

## Scrape an existing node-exporter

An exporter running on OPNsense can be scraped without a Juju relation. Its
collectors, including netstat, run **on the router** and are configured there.
The Alloy host exporter observes the Alloy machine.

```yaml
- name: opnsense-node
  targets: ['192.0.2.1:9100']
  metrics_path: /metrics
  scheme: http
  scrape_interval: 30s
  scrape_timeout: 10s
  insecure_skip_verify: false
  labels:
    device: '<device-name>'
    site: '<site-name>'
```

Save that list to a file and apply it:

```bash
juju config alloy-nm manual-metrics-jobs="$(cat ./metrics-jobs.yaml)"
```

Each job accepts only these fields:

| Field | Required/default | Meaning |
| --- | --- | --- |
| `name` | Required, unique | Scrape job name. |
| `targets` | Required, nonempty list | Prometheus exporter addresses, normally `host:port`. |
| `metrics_path` | `/metrics` | HTTP path to scrape. |
| `scheme` | `http` | `http` or `https`. |
| `scrape_interval` | Alloy default `60s` | Optional duration such as `30s`; the manual schema accepts a number followed by one unit. |
| `scrape_timeout` | Alloy default `10s` | Optional duration; must be valid for the chosen scrape interval. |
| `insecure_skip_verify` | `false` | For HTTPS, explicitly skips certificate verification when true. |
| `labels` | Empty mapping | Extra nonempty string labels. `juju_` labels are reserved and supplied by the charm. |

## All Juju configuration options

Use `juju config alloy-nm` to inspect current values and
`juju config alloy-nm OPTION=VALUE` to change one. Empty strings
below mean the option is unset. YAML examples above are passed as string values.

### Network discovery and SNMP

| Option | Type | Default | Purpose |
| --- | --- | --- | --- |
| `network-monitoring-enabled` | Boolean | `false` | Enable managed NetBox/SNMP collection. |
| `netbox-url` | String | Empty | HTTP(S) base URL, including any deployment path prefix. Required when enabled. |
| `netbox-token` | String | Empty | Granted secret reference with a `token` field. Required when enabled. |
| `snmp-auth` | String | Empty | Granted secret reference with a `snmp-yaml` field. Required when enabled. |
| `netbox-auth-scheme` | String | `Bearer` | `Bearer` or `Token`. |
| `netbox-ca` | String | Empty | Optional `ca-pem` secret for a certificate chain not already trusted by the collector. Publicly trusted certificates need none. |
| `netbox-site` | String | Empty | Optional exact site slug and default `site` label for forwarded metrics/logs. Existing source labels take precedence. |
| `netbox-region` | String | Empty | Optional region filter and default `region` label for forwarded metrics/logs. Existing source labels take precedence. |
| `netbox-tags` | String | Empty | Optional comma-separated tag slugs; all must match. |
| `netbox-status` | String | `active` | Device status slug; empty includes all statuses. |
| `collector-id` | String | Empty | Required name for the collection scope, emitted as the `collector` metric/rule label; it does not filter devices. |
| `netbox-refresh-interval` | String | `60s` | Positive inventory refresh duration. |
| `snmp-scrape-interval` | String | `30s` | Positive device scrape duration. |
| `snmp-scrape-timeout` | String | `15s` | Positive timeout, strictly shorter than the scrape interval. |
| `snmp-modules` | String | `system,if_mib` | Default embedded module list; per-device `snmp_modules` replaces it. |

### Alert shipping and presets

| Option | Type | Default | Purpose |
| --- | --- | --- | --- |
| `network-alerts-enabled` | Boolean | `true` | Publish the collector's metric rules. |
| `network-device-alerts-enabled` | Boolean | `false` | Include new device/interface rules when metric shipping is enabled. |
| `network-log-alerts-enabled` | Boolean | `false` | Publish parsed network event rules through Loki relations. |
| `network-alerts-config` | String | Empty | YAML presets and interface scopes; empty uses defaults with no interface scopes. |

### Logs and syslog

| Option | Type | Default | Purpose |
| --- | --- | --- | --- |
| `systemd-units` | String | Empty | Comma-separated host services whose journal logs should be forwarded. |
| `journal-kernel` | Boolean | `false` | Include host kernel messages in the broader journal capture. |
| `journal-match-expressions` | String | Empty | Newline-separated raw journal matches, for example `SYSLOG_IDENTIFIER=lxd`; passed through to journald. |
| `enable-syslogreceivers` | Boolean | `false` | Receive RFC5424 syslog on TCP/UDP port 1514. |
| `enable-syslog-rfc3164` | Boolean | `false` | Enable a separate RFC3164 TCP/UDP receiver, independently of RFC5424. |
| `syslog-rfc3164-port` | Integer | `1513` | RFC3164 port (1–65535); must differ from enabled RFC5424/Alloy HTTP listeners. |
| `syslog-parsers` | String | Empty | Optional comma-separated `opnsense_filterlog,cisco_ios`; empty disables parsing. |
| `syslog-drop-access-logs` | Boolean | `false` | Drop common HTTP request-style remote syslog lines. |
| `syslog-drop-expressions` | String | Empty | Newline-separated regexes for dropping remote syslog lines. |
| `syslog-rate-limit` | Integer | `0` | Forwarded remote lines/second after drops; `0` disables limiting. |
| `syslog-rate-burst` | Integer | `0` | Burst size; `0` uses the rate limit when limiting is enabled. |

### Local runtime and manual metrics

| Option | Type | Default | Purpose |
| --- | --- | --- | --- |
| `manual-metrics-jobs` | String | Empty | YAML list of external Prometheus jobs; needs a metrics destination. |
| `log-level` | String | `info` | Alloy logging: `debug`, `info`, `warn`, `error`; `warning` → `warn`, `critical` → `error`. |
| `custom_args` | String | `--server.http.listen-addr=127.0.0.1:6987` | Alloy service arguments. An explicit listener address controls management access. Changes require a restart. |
| `alloy-livedebugging` | Boolean | `false` | Enable live debugging; temporarily listens on `0.0.0.0:12345`; disabling restores previous arguments. |
| `config-override` | String | Empty | Complete Alloy configuration. Replaces generated pipelines and bypasses managed network settings, parsing and owned network alerts. Empty restores generation. |

## Existing integrations

Endpoint names, interfaces and relation libraries are unchanged:

| Endpoint | Interface | Purpose |
| --- | --- | --- |
| `send-remote-write` | `prometheus_remote_write` | Metrics destinations and Prometheus alert-rule delivery. |
| `send-loki-logs` | `loki_push_api` | Log destinations and Loki alert-rule delivery. |
| `metrics-endpoint` | `prometheus_scrape` | Scrape related Prometheus targets. |
| `machine-observability` | `machine_observability` | Collect colocated principals' metrics, journal/file logs and v3 rule artifacts. |
| `grafana-cloud-config` | `grafana_cloud_config` | Authenticated Grafana Cloud metrics/log destinations. |
| `syslog-receiver` | `syslog` | Publish receiver connection details to related senders. |
| `alloy-peers` | `alloy_peers` | Internal peer state. |

Machine-observability accepts schema v2/v3 with `source_topology`. V1 payloads
block because this principal charm cannot infer the source identity. V3 adds
bounded Prometheus/Loki rule artifacts validated with packaged `cos-tool`.
Malformed input retains its last valid rules; valid omission or relation removal
withdraws that provider's rules. Collector alert flags do not affect providers.

For colocated principals:

```bash
juju relate alloy-nm:machine-observability op-node:machine-observability
juju relate alloy-nm:machine-observability op-reth:machine-observability
```

If you use gateways, retain their separate backend and rule-forwarding relations:

```bash
juju relate alloy-nm:send-remote-write mimir-gateway-vm:receive-remote-write
juju relate mimir-gateway-vm:mimir-alert-rules mimir-vm:receive-remote-write
juju relate mimir-gateway-vm:backend mimir-vm:backend
juju relate alloy-nm:send-loki-logs loki-loadbalancer-vm:loki_push_api
juju relate loki-loadbalancer-vm:loki-alert-rules loki-vm:loki_push_api
juju relate loki-loadbalancer-vm:ingress loki-vm:ingress
```

For Grafana Cloud, relate your configured integrator:

```bash
juju relate alloy-nm:grafana-cloud-config grafana-cloud-integrator:grafana-cloud-config
```

Cloud destinations are additive to plain remote-write/Loki relations. Credentials
come from the integrator, using signal-specific credentials when present and
legacy shared credentials otherwise. `update-status` reports failed Cloud
connectivity probes. Automatic rule shipping uses the two rule-capable relations
above; the Cloud config relation supplies endpoints and credentials only.

Dashboards relate directly from their providers to Grafana. They do not pass
through Alloy. Metrics and logs collected for related providers retain their
Juju identity; local, manual, SNMP and incoming syslog telemetry carries collector
identity. SNMP `instance` is the management IPv4; `netbox_id` remains stable across
renames and address changes.

## Troubleshooting and lifecycle

Start with these backend queries:

```promql
up{job="snmp/network"}
{job="snmp/network", __name__=~"ifHC.*"}
up{job="snmp/network", snmp_config_error="invalid_modules"}
```

- No targets: check filters, NetBox discovery errors, primary IPv4 and the
  selected authentication profile. Unknown profiles are excluded.
- `up=0`: check reachability, SNMP credentials/view and the module list. Invalid
  lists have the error label above; successful discovery alone does not prove SNMP.
- `up=1` with missing metrics: verify the device supplies the OIDs in the chosen
  module. Health rules cannot evaluate metrics that the device does not return.
- Logs without parsed fields: check `syslog-parsers`, the sender's application
  name and message format. The original line remains available for comparison.
- Missing alerts: check the family flags, interface scopes, parser requirements
  and ruler configuration. Forwarding metrics/logs is separate from evaluating rules.

Secret rotation keeps the same reference:

```bash
juju update-secret secret:NETBOX-ID 'token#file=./netbox-token.txt'
juju update-secret secret:SNMP-ID 'snmp-yaml#file=./snmp_auth.yaml'
```

Invalid candidates retain the last applied generation. A lost secret grant
withdraws managed network collection and retires its files while other telemetry
continues. Discovery outages retain Alloy's previous targets; a successful empty
response withdraws them. Inventory changes and most configuration changes use
reloads. Runtime argument changes require a restart.

Charm refresh preserves the running process and storage; a normal subsequent
configuration/secret event applies new settings. Existing units need an Alloy
version supporting the generated components. The remote-write WAL keeps its
component identity and a 30-minute retention bound. Longer destination outages
can lose old samples.

See the [operator guide](docs/network-monitoring.md) for failure handling and
alert investigations, [architecture](docs/charm-architecture.md) for ownership
and lifecycle, [DEVELOPING.md](DEVELOPING.md) for dependencies, and
[verification instructions](docs/build-test-deploy.md) for build/live checks.
