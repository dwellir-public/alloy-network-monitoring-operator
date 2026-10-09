Yes. I would make this a **separate monitoring integration that consumes NetBox inventory**, rather than adding monitoring responsibilities to `netbox-hardware-sync`.

The two projects would complement each other:

```text
netbox-hardware-sync
    Devices → NetBox
    Manually invoked
    Updates selected inventory fields

alloy-network-monitoring
    NetBox → target discovery
    Devices → SNMP metrics → Mimir
    Runs continuously
    Never modifies inventory
```

The first monitoring release should achieve one clear result:

> **Enroll a device in NetBox, and the appropriate site’s Alloy collector starts monitoring it without editing a target file or restarting Alloy.**

Interface-description enrichment should be the next milestone, not a prerequisite for getting discovery working.

# 1. Architecture and ownership

I would separate the system into three responsibilities:

| Responsibility | Owner |
|---|---|
| What devices exist, where they belong, and whether monitoring is enabled | NetBox |
| Collecting and forwarding telemetry | Alloy |
| Storing metrics, evaluating queries, and serving dashboards | Mimir and Grafana |

Your hardware synchronization script maintains selected NetBox inventory fields. It does not generate Alloy configuration, provision SNMP credentials, or manage dashboards.

Likewise, Alloy must not update interface descriptions, device status, or other inventory fields based on scrape results.

## Proposed topology

```text
                    External / central infrastructure
                 ┌──────────────────────────────────────┐
                 │ NetBox                               │
                 │                                      │
                 │ Device inventory                     │
                 │ Monitoring enrollment                │
                 │ Site and authentication-profile names│
                 │ Prometheus service-discovery endpoint│
                 └───────────────────▲──────────────────┘
                                     │
                      Outbound HTTPS discovery requests
                                     │
       ┌─────────────────────────────┴────────────────────────┐
       │ Site: stockholm-1                                    │
       │                                                      │
       │  Alloy                                               │
       │    ├── Discovers local targets from NetBox            │
       │    ├── Polls devices using SNMPv3                     │
       │    ├── Applies the agreed metric labels               │
       │    └── Sends metrics to the configured Mimir ingress  │
       │            │                         │                │
       │         SNMPv3                  Remote write          │
       │            ▼                         ▼                │
       │    OPNsense / Cisco              Local Mimir          │
       └──────────────────────────────────────────────────────┘
```

NetBox does not need to contact Alloy or reach your management networks. All discovery requests originate from the collector.

The proposed network access is deliberately narrow:

| Source | Destination | Purpose |
|---|---|---|
| Alloy host | NetBox HTTPS endpoint | Read target inventory |
| Alloy host | Selected management addresses, UDP/161 | SNMP polling |
| Alloy host | Mimir ingress | Remote write |
| Administrative network | Alloy management interface, where enabled | Troubleshooting |

The collection path does not require a publicly exposed Alloy listener.

# 2. Define v0.1 before adding features

## Initial scope

| Item | v0.1 decision |
|---|---|
| Deployment | One active Alloy collector for one site |
| Test devices | One OPNsense; add one Cisco after the first works |
| Discovery | NetBox Prometheus service-discovery plugin |
| Enrollment | NetBox tag: `snmp` |
| Collection scope | One explicit NetBox site |
| Authentication | SNMPv3 profiles stored outside NetBox |
| Modules | Your existing `system,if_mib` |
| Destination | Configured local Mimir ingress |
| Labels | Small, explicit device-label contract |
| Interface names | Preserve the names SNMP actually reports |
| Inventory writes | None |

Do not initially add automatic network scanning, SNMP traps, custom vendor modules, interface enrichment, multi-collector HA, or a custom discovery service.

**The first version can be predominantly Alloy configuration and tests—not another Python application.** Alloy provides HTTP discovery, target relabeling, an embedded SNMP exporter, scraping, and remote write. [Grafana Labs](https://grafana.com/docs/alloy/latest/reference/components/discovery/discovery.http/)

# 3. NetBox’s role: monitoring enrollment, not exporter configuration

## Minimal device metadata

I would start with:

| NetBox field | Example | Purpose |
|---|---|---|
| Device name | `opn-r2` | Human-readable identity |
| Device ID | `123` | Stable inventory reference |
| Primary IPv4 | `10.99.100.253` | Initial SNMP management address |
| Site | `home` | Collector selection |
| Status | `active` | Initial eligibility policy |
| Tag | `snmp` | Explicit monitoring enrollment |
| Custom field: `snmp_auth` | `opnsense` | Selects a locally available credential profile |

The example ID is illustrative.

Keep `snmp_auth` as a controlled choice rather than arbitrary text. For the first release, allow:

```text
opnsense
cisco
```

Do not add `snmp_modules` yet unless devices actually need different module selections. Your first two devices can share the same module set.

Later, additional module selections can become a controlled custom field without changing the overall architecture.

## Separate the two enrollment tags

I would intentionally keep:

```text
hardware-sync
snmp
```

separate.

A device may be monitored without being enrolled in automatic inventory updates. Conversely, a device may be synchronized into NetBox without supporting SNMP.

Neither tag should implicitly enable the other workflow.

## Management-address policy

For v0.1, use one documented field—such as primary IPv4—and fail to select a target when that field is absent.

Do not implement an implicit chain such as:

```text
primary IP → OOB IP → guessed DNS name → device name
```

For HA pairs, use each member’s dedicated management address, not a shared CARP VIP. Our intended inventory association is:

```text
One NetBox device → one specific physical or virtual appliance
```

A future explicit `snmp_address` override is reasonable, but only add it when a real device needs it.

# 4. Discovery: Alloy pulls a filtered target list

## Preferred approach

Use the existing **`netbox-plugin-prometheus-sd`** plugin on NetBox.

It exposes devices in Prometheus HTTP service-discovery format, reuses NetBox filtering and permissions, and returns an unpaginated target list. It does not collect SNMP data. [GitHub](https://raw.githubusercontent.com/FlxPeters/netbox-plugin-prometheus-sd/main/README.md)

For a Stockholm collector, the query would be:

```text
/api/plugins/prometheus-sd/devices/?status=active&tag=snmp&site=stockholm-1
```

For your lab:

```text
/api/plugins/prometheus-sd/devices/?status=active&tag=snmp&site=home
```

I would begin with a **60-second discovery refresh**. This is an inventory-refresh interval, independent of the SNMP scrape interval.

## Site, region, and collector are different concepts

I would define:

```text
site       = where NetBox says the device belongs
collector  = the monitoring scope responsible for it
region     = an optional geographical or organizational grouping
```

Initially, site and collector can correspond one-to-one:

```text
site="stockholm-1"
collector="stockholm-1"
```

The `region="stockholm-1"` label you suggested can also be emitted as an explicit compatibility choice, but document that meaning rather than assuming it comes from NetBox’s Region object.

If one collector later covers several sites, introduce a field such as:

```text
snmp_collector="sweden-central"
```

Do not require that extra field while a simple site filter is sufficient.

**Site filtering is an operational selection rule, not network isolation.** I would also restrict the collector’s network access and NetBox account permissions.

## Hosting prerequisite and fallback

Before implementation, verify that your externally hosted NetBox permits installing the plugin, and pin a version tested against your installed NetBox release.

If plugins are unavailable, use a small local exporter of **target files**:

```text
NetBox REST API
      ↓
Manually invoked target-export command
      ↓
Atomically written HTTP-SD-compatible JSON file
      ↓
Alloy discovery.file
```

That is a fallback, not another mandatory component. Alloy’s SNMP exporter supports targets supplied through file discovery as well. [Grafana Labs](https://grafana.com/docs/alloy/latest/reference/components/prometheus/prometheus.exporter.snmp/)

# 5. Alloy’s internal pipeline

I would use this component sequence:

```text
discovery.http
      │
      ▼
discovery.relabel
  Select and normalize NetBox devices
      │
      ▼
prometheus.exporter.snmp
  Build scrape targets for the embedded exporter
      │
      ▼
discovery.relabel
  Normalize final job and instance labels
      │
      ▼
prometheus.scrape
      │
      ▼
prometheus.relabel
  Apply explicit metric filtering, if required
      │
      ▼
prometheus.remote_write
      │
      ▼
Mimir
```

The two relabel stages have different jobs.

## Before the exporter: normalize inventory

Translate discovery metadata into a narrowly defined SNMP target.

For example:

```json
{
  "name": "netbox-device-123",
  "address": "10.99.100.253",
  "module": "system,if_mib",
  "auth": "opnsense",
  "netbox_id": "123",
  "device": "opn-r2",
  "site": "home",
  "collector": "lab"
}
```

This is our proposed **normalized target contract**, not the raw plugin response.

The plugin may initially identify a target by its device name; its device serializer also supplies IP metadata. We should deliberately select the management address rather than assume that the returned target name resolves correctly. [GitHub](https://raw.githubusercontent.com/FlxPeters/netbox-plugin-prometheus-sd/main/netbox_prometheus_sd/api/serializers.py)

Only promote approved metadata fields. For example:

```text
__meta_netbox_id
    → netbox_id

__meta_netbox_name
    → device

__meta_netbox_site_slug
    → site

__meta_netbox_custom_field_snmp_auth
    → auth
```

Internal discovery labels need explicit mapping to become retained metric labels. Do not broadly promote every `__meta_netbox_*` field. [Grafana Labs](https://grafana.com/docs/alloy/latest/reference/components/discovery/discovery.relabel/)

The normalizer should reject targets with missing identity/address information and permit only known authentication profiles. It must never silently fall back to public SNMP credentials.

## After the exporter: normalize scrape identity

This is an important implementation detail: Alloy’s SNMP exporter constructs its own scrape targets and modifies their `job` labels using the target name. [GitHub](https://raw.githubusercontent.com/grafana/alloy/main/internal/component/prometheus/exporter/snmp/snmp.go)

Therefore, explicitly normalize the resulting targets to our contract:

```text
job="snmp/network"
instance="netbox-device-123"
```

Do not rely solely on `job_name` in `prometheus.scrape`.

Keep the exporter-generated internal address and query parameters intact at this stage. They direct the scrape to Alloy’s embedded exporter; they are not the router’s HTTP endpoint.

# 6. Metric labels and SNMP configuration

## Label contract

I would start with:

| Label | Intended meaning |
|---|---|
| `job` | `snmp/network` |
| `instance` | Stable collector-independent device identifier |
| `netbox_id` | NetBox DCIM device ID |
| `device` | Current NetBox device name |
| `site` | NetBox site slug |
| `collector` | Responsible monitoring scope |
| `ifName` | Original SNMP interface name |
| `ifDescr` | Original SNMP description |
| `ifAlias` | Original SNMP alias, when supplied |
| `ifIndex` | Original device-local SNMP index |

An illustrative metric would be:

```text
ifHCInOctets{
  job="snmp/network",
  instance="netbox-device-123",
  netbox_id="123",
  device="opn-r2",
  site="home",
  collector="lab",
  ifName="vtnet2",
  ifDescr="vtnet2",
  ifIndex="3"
}
```

**Do not overwrite `ifDescr` with `PFSYNC`.** Preserve the original observations, and add human-readable inventory metadata separately later.

Also avoid copying rack comments, contact emails, arbitrary custom fields, or whole tag lists onto every counter. Our initial label set should answer “which device, which site, which interface?” without becoming a copy of the inventory database.

## Credentials and modules

Keep authentication profiles in a workload-readable secret file:

```text
/etc/alloy/snmp.yaml
```

NetBox contains only the profile name. Each site can resolve that name to its own credentials.

Continue using the merge strategy for adding local authentication profiles to the embedded configuration. The exporter supports dynamically supplied targets and locally provided authentication configuration. [Grafana Labs](https://grafana.com/docs/alloy/latest/reference/components/prometheus/prometheus.exporter.snmp/)

For the standard modules, do not introduce a MIB-generator workflow yet. Vendor MIBs and generated custom modules belong in a later, version-controlled collection-profile milestone. The upstream exporter distinguishes its ready-made configuration from configuration generated for custom walks and vendor MIBs. [GitHub](https://github.com/prometheus/snmp_exporter)

## Preserve useful collection diagnostics

I would remove the blanket policy of dropping:

```text
^snmp_.*
```

during the proof of concept.

We need collection diagnostics to distinguish a slow device, an authentication problem, and an overloaded collector. Start by retaining them; remove specific metrics only after identifying an actual cost or cardinality problem.

Your existing `30s` scrape interval and `15s` timeout are reasonable **initial test settings**, not guarantees. Validate them against both devices, including their retry behavior, before deploying them across a site.

# 7. Mimir: explicit destination and durable buffering

Each collector should write to its configured private Mimir ingress. Do not make the destination depend on an arbitrary device custom field.

Mimir’s native remote-write endpoint is:

```text
/api/v1/push
```

The tenant header is required when multi-tenancy is enabled, unless an authenticating proxy supplies it. [Grafana Labs](https://grafana.com/docs/mimir/latest/references/http-api/)

The deployment contract should provide:

```text
Full remote-write URL
Authentication method and secret reference
Tenant configuration, where applicable
Trusted CA configuration
```

A tenant header is not, by itself, authentication. Mimir’s documentation recommends an authentication layer such as a reverse proxy for protecting access and assigning tenant identity. [Grafana Labs](https://grafana.com/docs/mimir/latest/manage/secure/authentication-and-authorization/)

## Runtime state

Preserve Alloy’s storage directory across restarts and charm upgrades.

Remote write uses a write-ahead log, but buffering is finite and retention settings can cause unsent data to be discarded during extended failures. It is not an unlimited guarantee against data loss. [Grafana Labs](https://grafana.com/docs/alloy/latest/reference/components/prometheus/prometheus.remote_write/)

Our deployment requirements should therefore include persistent storage, disk monitoring, a documented buffering policy, and stable component names.

For the lab, I would use an isolated test tenant or clearly isolated test labels so we can compare pipelines without interfering with existing dashboards.

# 8. Interface descriptions: the second release

Your manual synchronization tool can populate:

```text
NetBox device: opn-r2
Interface:    vtnet2
Description:  PFSYNC
```

However, **device discovery does not automatically enrich each interface counter**. The plugin’s device endpoint provides device-level target metadata, not an interface-description lookup applied to individual SNMP samples. [GitHub](https://raw.githubusercontent.com/FlxPeters/netbox-plugin-prometheus-sd/main/netbox_prometheus_sd/api/serializers.py)

I would solve this separately.

## Proposed metadata path

```text
Manual hardware sync
    OPNsense → NetBox interfaces

Read-only metadata export
    NetBox interfaces → interface information metrics

Monitoring
    SNMP counters + information metrics → dashboard queries
```

For example, publish:

```text
netbox_interface_info{
  netbox_id="123",
  ifName="vtnet2",
  interface_description="PFSYNC"
} 1
```

Then queries can join on:

```text
(netbox_id, ifName)
```

rather than globally replacing every `vtnet2`.

A matched-interface query could look like:

```promql
(
  8 * rate(ifHCInOctets{job="snmp/network"}[5m])
)
* on (netbox_id, ifName) group_left(interface_description)
  netbox_interface_info
```

PromQL supports this many-to-one label enrichment through vector matching. The metadata side must contain only one matching series per key. [Prometheus](https://prometheus.io/docs/prometheus/latest/querying/operators/)

That example shows matching interfaces only. Production dashboards should include a fallback to the original interface name when metadata is missing.

## No permanent custom service required initially

For this later milestone, a small command can read NetBox, write a `.prom` file atomically, and exit. Alloy’s Unix exporter has a textfile collector for exposing metrics from such files. [Grafana Labs](https://grafana.com/docs/alloy/latest/reference/components/prometheus/prometheus.exporter.unix/)

Initially run the metadata-export command manually after inventory synchronization; add a timer only when useful.

Keep it read-only and separate from the hardware-sync writer.

The metadata workflow must account for duplicate mappings, exact interface-name agreement, failed refreshes, and freshness. Descriptions still reflect the most recent inventory synchronization—not necessarily the device’s live configuration.

# 9. Failure behavior must be designed and tested

| Situation | Required interpretation |
|---|---|
| SNMP device unreachable | Collection failure; do not change NetBox device status |
| Missing or invalid profile | Configuration/enrollment problem; no fallback credentials |
| NetBox discovery fails | Discovery problem, distinct from device health |
| NetBox successfully returns an empty list | No selected targets; do not treat this as a transport error |
| Mimir unavailable | Forwarding problem; inspect buffering and backlog |
| Interface metadata unavailable | Keep raw SNMP monitoring usable |

## NetBox outage versus collector restart

HTTP discovery errors do not publish a replacement target set; previously discovered targets can remain active in the running process. A successful empty response, however, removes previously returned targets. That distinction is visible in the upstream HTTP discovery implementation. [GitHub](https://raw.githubusercontent.com/prometheus/prometheus/main/discovery/http/http.go)

Do not assume those discovered targets constitute a persistent on-disk cache. Test a cold start with NetBox unavailable and document the result.

For v0.1, accepting that startup dependency is reasonable. A persisted discovery snapshot can become a later resilience feature.

## Monitor the monitoring pipeline

I would cover four areas: discovery failures, device scrape failures, remote-write delivery/backlog, and collector availability.

Do not rely only on an Alloy component appearing healthy. For example, HTTP discovery exposes a refresh-failure counter separately from its configuration health. [Grafana Labs](https://grafana.com/docs/alloy/latest/reference/components/discovery/discovery.http/)

Also distinguish **a failed target** from **a disappeared target**. An alert that only evaluates existing `up == 0` series is not sufficient for a collector whose entire target list vanishes.

Start with an explicit “no network targets for this collector” safeguard and review monitoring-enrollment changes.

# 10. Project structure and Juju integration

For the proof of concept, keep the repository small:

```text
alloy-network-monitoring/
├── README.md
├── .gitignore
├── versions.toml
│
├── config/
│   ├── config.alloy
│   └── snmp.example.yaml
│
├── docs/
│   ├── architecture.md
│   └── label-contract.md
│
├── alerts/
│   └── network.rules.yaml
│
└── tests/
    ├── fixtures/
    │   ├── netbox-valid.json
    │   ├── netbox-other-site.json
    │   ├── netbox-missing-address.json
    │   └── netbox-empty.json
    │
    ├── test_discovery_contract.py
    └── test_pipeline.py
```

`versions.toml` records the versions actually tested: Alloy, NetBox, the discovery plugin, and device software.

The Python files are test tooling—not a new production collector.

Do not commit real credentials, live configuration containing secrets, or the earlier credential-containing debug logs.

## Move the working configuration into the existing charm

Once the standalone pipeline passes its tests, integrate it into your Alloy charm rather than maintaining two independent implementations.

The proposed additions would be roughly:

```text
existing-alloy-charm/
├── src/
│   └── network_monitoring.py
├── templates/
│   └── network-snmp.alloy.j2
└── tests/
    └── network_monitoring/
```

These are proposed additions, not claims about the current charm’s source or supported interfaces.

The charm should own deployment: configuration validation, secret-file permissions, rendering, reloads, and persistent workload storage. **Alloy—not charm hooks—should perform recurring discovery.**

Proposed configuration inputs include:

```text
network-monitoring-enabled
netbox-url
netbox-site
collector-id
netbox-token-secret-id
snmp-auth-secret-id
```

Reuse the charm’s existing remote-write integration where available rather than inventing another Mimir connection mechanism.

Use a separate read-only NetBox token for Alloy, not the hardware-sync tool’s write token. NetBox supports tokens with writes disabled and optional source-IP restrictions. [NetBox Documentation](https://netbox.readthedocs.io/en/stable/models/users/token/)

## Safe configuration changes

The charm should render a candidate, validate it, and only then replace the active configuration and request a reload.

`alloy validate` is useful for configuration checks, but its documented scope does not prove external authentication, discovery behavior, or successful metric ingestion. Those need integration tests. [Grafana Labs](https://grafana.com/docs/alloy/latest/reference/cli/validate/)

# 11. Implementation milestones and acceptance tests

| Milestone | Deliverable | Acceptance |
|---|---|---|
| **A — Static baseline** | One lab target → Alloy → Mimir | Expected interface metrics are queryable |
| **B — NetBox discovery** | Replace the static target with filtered HTTP discovery | Enrollment changes take effect without editing Alloy |
| **C — Labels and failures** | Final identity contract and operational checks | Correct labels; empty, invalid, and unavailable discovery behave as documented |
| **D — Charm integration** | Deploy the tested pipeline through the existing charm | Reloads, restarts, and secret updates preserve the intended behavior |
| **E — Interface metadata** | Read-only NetBox interface export and query enrichment | `vtnet2` displays as `PFSYNC`; missing metadata does not hide raw monitoring |
| **F — Multiple collectors** | Deliberate HA or sharding design | No unintended duplicate collection or inconsistent target ownership |

For milestones B–D, test at least these cases:

```text
Enroll a second device.
Remove its monitoring tag.
Return a device from the wrong site.
Return a device without a management address.
Use an unknown authentication profile.
Make NetBox temporarily unavailable.
Restart Alloy while NetBox is unavailable.
Interrupt Mimir delivery.
Change a NetBox device name.
Confirm job and instance after the exporter.
```

Use the actual Alloy binary in integration tests. Testing a Python approximation of its relabeling rules would not establish that the deployed pipeline behaves correctly.

## Defer collector HA deliberately

Do not simply add a second Alloy unit with identical targets.

Later choose either coordinated scrape sharding or redundant collection with explicitly configured Mimir HA deduplication. Mimir’s deduplication depends on its HA tracker and matching cluster/replica labels; it does not automatically deduplicate arbitrary duplicate collectors. [Grafana Labs](https://grafana.com/docs/mimir/latest/configure/configure-high-availability-deduplication/)

Similarly, during migration, do not run both old and new collectors against the same targets into indistinguishable series. Compare in isolation, adapt dashboards to the label contract, then cut over.

---

## Recommended first deliverable

For now, build only:

```text
NetBox: active device + snmp tag + site + auth-profile name
                              ↓
                  Alloy HTTP discovery
                              ↓
                  Normalized SNMP target
                              ↓
                   Embedded SNMP exporter
                              ↓
                    Consistent metric labels
                              ↓
                         Local Mimir
```

**The hardware-sync project keeps inventory accurate. This integration makes monitoring follow that inventory. Neither project needs to become responsible for the other’s lifecycle.**
