# Alloy network monitoring architecture

The charm orchestrates configuration, Juju secrets, service control and existing
integrations. Alloy performs continuous inventory discovery, SNMP collection and
forwarding. There is no hook-time inventory client or inventory writer.

Syslog uses a shared relabeler and processor for two independently enabled
components: RFC5424 `receiver` on TCP/UDP 1514 and RFC3164 `receiver_rfc3164`
on configurable TCP/UDP 1513 by default. Both retain the same source identity,
location defaults, body parsers, drops, shared rate limit and alert selection.
The `syslog-receiver` integration continues to advertise RFC5424 only. Invalid
RFC3164 port ranges or conflicts are rejected before validation/apply, retaining
the previous generation. Configuration changes reload Alloy; charm refresh
preserves the running generation until subsequent reconciliation.

```mermaid
flowchart LR
    NetBox[NetBox HTTP discovery] --> HTTP[discovery.http]
    HTTP --> Inventory[discovery.relabel: eligible inventory]
    Inventory --> SNMP[prometheus.exporter.snmp]
    Secrets[Juju secret profiles] --> Files[Protected local.file inputs]
    Files --> SNMP
    Files --> HTTP
    SNMP --> Identity[discovery.relabel: stable identity]
    Identity --> Scrape[prometheus.scrape]
    Scrape --> Writer[prometheus.remote_write.metrics]
    Writer --> Mimir[Existing remote-write destination]
```

[network_monitoring.py](../src/network_monitoring.py) validates public settings
and named SNMPv2c/v3 profiles and renders the network component graph. Each
NetBox device selects its named profile; the protocol comes from the secret.
V2c community profiles and v3 credential profiles share the existing protected
secret-file and rotation path, without adding a relation/config interface.
[secret_files.py](../src/secret_files.py) stages root-owned credential generations.
[charm.py](../src/charm.py) resolves granted secret revisions and applies a
validated generation, restoring previous files and service settings on failure.
Applied state advances after service application succeeds.
[alloy.py](../src/alloy.py) controls the service and atomically writes protected
files. [config_builder.py](../src/config_builder.py) composes the graph with
existing telemetry sources and destinations.

All relation definitions and vendored libraries retain their previous contracts.
Machine-observability inputs preserve each provider's source topology. Related
and manual metrics use the shared existing writer; logs use existing Loki
outputs. Device samples use NetBox identity, target-IP instances and collector
Juju topology as collection provenance. The discovered NetBox role slug is kept
as the optional `role` target/metric label; it is not inferred for logs or other
scrape jobs. Incoming syslog and local host journal
logs receive collector topology; provider metrics/logs retain their own source
topology. External scrape jobs without provider identity receive collector
context, while partial provider topology is preserved without invented fields.
Local host and Alloy self metrics retain collector Juju topology. Self metrics
keep the last applied collector label during secret withdrawal.

Configured `netbox-site`/`netbox-region` slugs become optional external-label
defaults on the shared metric and log writers. Existing source labels retain
precedence. This also applies when managed NetBox/SNMP collection is disabled;
the slugs then label the collection scope without performing discovery. When
both defaults are present, syslog transport protocol becomes structured metadata
to preserve the default Loki label budget. Empty defaults leave existing streams
unchanged; a complete configuration override retains its own label policy.

The existing v3 rule pipeline continues to validate, cache and forward owned
Prometheus/Loki artifacts. [network_alerts.py](../src/network_alerts.py) adds
collector-scoped Prometheus groups and optional parsed-event Loki groups through
the same bounded `alert_rules` payload and encoding negotiation.
[network_alert_settings.py](../src/network_alert_settings.py) validates the
independent publication controls, named interface scopes and bounded preset YAML.
The charm records these public settings after successful application; invalid
candidates retain both the running graph and the prior rule selections. Related
provider ownership remains separate. It adds no relation fields. Dashboards still pass
directly from their providers to Grafana.

[snmp_modules.py](../src/snmp_modules.py) pins the allowed embedded module names.
Global defaults and canonical per-device NetBox lists feed Alloy's exporter.
Invalid per-device lists remain observable failed targets rather than silently
falling back or disappearing. On `update-status`, the charm reads the public
`discovery.relabel.network_scrape` output through Alloy's local API and reports
invalid device IDs as blocked. This bounded check reads no credential component
or second NetBox inventory source; valid targets continue collecting. An
unavailable/malformed/oversized response yields waiting status rather than a
successful validation claim. [network_syslog.py](../src/network_syslog.py)
renders optional OPNsense/Cisco body parsers inside the existing remote processor.
Parsed fields become structured metadata, with no extra indexed labels or message
rewrite. Receiver filtering and rate limiting follow parsing.

The writer name and package storage configuration remain stable. Charm refresh
skips workload installation, rewriting and restarting in `upgrade-charm`.
Normal configuration reconciliation can subsequently restart Alloy when runtime
arguments change, including migration to a new default listener; this is logged
and signalled in status. With unchanged arguments, logging, profile and label
changes apply through reload. The default management listener is loopback.
Discovery changes happen in Alloy without charm events or restarts. Missing
destinations disable network collection; a configured destination outage uses
the bounded remote-write WAL. See the [operator guide](network-monitoring.md) for
filters, profiles, labels and failure behavior.
