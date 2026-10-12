# NetBox and SNMPv3 monitoring

`alloy-network-monitoring` installs Grafana Alloy and continuously reads device
inventory from NetBox's Prometheus service-discovery plugin. Alloy polls selected
devices using its embedded SNMP exporter and forwards samples to the existing
remote-write destinations. It never writes to NetBox.

## Configure a collector

Deploy one unit per application and choose a unique, stable `collector-id`.
Multiple applications may cover different scopes. Overlapping scopes collect
the same devices independently; HA coordination is a later milestone.

Connect the existing `send-remote-write` or `grafana-cloud-config` integration
before enabling network collection. Existing endpoint names, interfaces, data
schemas, libraries and sink authentication behavior are preserved.

Create two user-owned Juju secrets. The `netbox-token` config option references a
secret with a `token` field. The `snmp-auth` config option references a secret
with a `snmp-yaml` field containing the complete `auths` YAML mapping. These are
secret references in config, rather than credentials.

Copy the [SNMPv3 secret template](../examples/example.snmp_auth.yaml) outside the checkout,
replace its placeholders with the credentials configured on your devices, and
save the NetBox token in a separate local file. Juju's
[file input syntax](https://documentation.ubuntu.com/juju/3.6/reference/juju-cli/list-of-juju-cli-commands/add-secret/)
reads those files without placing credentials in command arguments:

```bash
juju add-secret netbox-token 'token#file=./netbox-token.txt'
juju add-secret snmp-auth 'snmp-yaml#file=./snmp_auth.yaml'

# Substitute the secret IDs returned by the two commands above.
juju grant-secret secret:NETBOX-ID alloy-network-monitoring
juju grant-secret secret:SNMP-ID alloy-network-monitoring
juju config alloy-network-monitoring \
  network-monitoring-enabled=true \
  collector-id=lab \
  netbox-url=https://netbox.example \
  netbox-token=secret:NETBOX-ID \
  snmp-auth=secret:SNMP-ID
```

The example deliberately leaves site, region and tags empty. Each is optional
and independent. Set any desired filters:

```bash
juju config alloy-network-monitoring \
  netbox-site=home netbox-region=sweden netbox-tags=snmp,edge
```

Empty filters impose no restriction. Multiple tags use AND: both `snmp` and
`edge` must be present. The status filter defaults to `active`; set
`netbox-status=""` to include all statuses. Region selection uses NetBox's device
filter. Verify descendant-region behavior against your installed NetBox version.
The plugin does not supply per-device region metadata, so region is not emitted
as a metric label.

The discovery URL appends `/api/plugins/prometheus-sd/devices/` to `netbox-url`,
preserving deployment path prefixes and encoding only configured filters.
`netbox-auth-scheme=Bearer` is the default for NetBox v2 tokens; use `Token` for
v1 tokens. A private CA can be supplied through `netbox-ca`, referencing a
granted secret with a `ca-pem` certificate bundle. Certificate verification
remains enabled.

The secret's `token` field contains only the raw token, without a `Bearer` or
`Token` prefix. The charm adds the configured authentication scheme. For a
private NetBox CA:

```bash
juju add-secret netbox-ca 'ca-pem#file=./netbox-ca.pem'
juju grant-secret secret:CA-ID alloy-network-monitoring
juju config alloy-network-monitoring netbox-ca=secret:CA-ID
```

## Multiple authentications

One secret supports up to 128 named SNMPv3 profiles. Each NetBox device's
`snmp_auth` custom field selects a name such as `opnsense` or `cisco`. Names are
configurable; separate profiles can serve vendors, device groups or individual
devices. Profiles support `noAuthNoPriv`, `authNoPriv` and `authPriv`; passwords
for authenticated modes must contain at least eight characters. SNMPv1/v2
communities, duplicate YAML keys, custom modules and unknown fields are rejected.

The secret contains credentials; NetBox contains only the profile name. Devices
with an unknown profile are excluded. There is no fallback to embedded public
credentials. Every device uses the embedded `system,if_mib` modules, merged with
the supplied authentication profiles.

To rotate either secret, update its existing Juju secret ID, keeping the config
reference:

```bash
juju update-secret secret:NETBOX-ID 'token#file=./netbox-token.txt'
juju update-secret secret:SNMP-ID 'snmp-yaml#file=./snmp_auth.yaml'
```

The charm consumes the latest granted revision on `secret-changed` and
`update-status`. Credentials are staged in root-owned generations under
`/etc/alloy/network-secrets/`, with directories `0750` and files `0640`, group
`alloy`. Managed network configuration and charm state contain file references,
rather than network credential contents or hashes. `local.file.is_secret` redacts credentials from
Alloy's component UI. Immutable generations avoid changing live files before
the complete replacement configuration has been validated. Managed network
reloads use Alloy's synchronous local API; restarts wait for its readiness
endpoint before advancing applied state. Existing service control remains in
place for configurations outside the managed network path.

## Eligibility and labels

A selected device must have a positive device ID, a name, a valid primary IPv4
address and a profile present in the secret. Configured site, status and tag
filters are checked again in Alloy. Region filtering relies on NetBox.
Raw metadata and incoming internal routing labels are discarded before the
embedded exporter constructs its own scrape targets.

Samples use `job="snmp/network"`, `instance` set to the target's primary IPv4
(for example, `10.99.100.254`), `netbox_id`, `device`, actual `site`, and
`collector`. SNMP samples also carry `juju_model`, `juju_model_uuid`,
`juju_application`, `juju_unit` and `juju_charm` for the collector. These identify
where collection happened; NetBox labels identify the device. Original `ifName`, `ifDescr`, `ifAlias` and
`ifIndex` labels remain exporter observations. SNMP diagnostic metrics are kept.
Local host and Alloy self metrics retain existing Juju topology and gain the
collector label while the network pipeline is configured.

Incoming syslog and host journal logs receive collector Juju topology too,
while retaining their original hostname, address and journal fields. Related
workload metrics and logs preserve the provider's Juju identity. For an external
scrape without provider topology, collector topology supplies the context.
Partial provider topology is retained as supplied; unknown fields are not
filled with a different application's identity. A full `config-override` owns
its label policy.

Syslog retains device hostname/address, application, facility, level/severity,
protocol and collector topology as indexed labels. Component, receiver
hostname/address, connection hostname and process ID move to structured metadata
to fit Loki's default 15-label limit. They remain available with LogQL pipeline
filters, for example `{log_source="remote_syslog"} | syslog_proc_id="123"`.
The explicit `service_name="loki.source.syslog"` retains the previous inferred
service name. This requires a Loki backend that accepts structured metadata;
the current lab supports it. See [Loki limits](https://grafana.com/docs/loki/latest/configure/)
and [Alloy structured metadata](https://grafana.com/docs/alloy/latest/reference/components/loki/loki.process/).

`netbox_id` remains the inventory identity when a device is renamed or its
address changes. `instance` follows address changes. Updating from the original
`netbox-device-<id>` instance convention creates new metric series; historical
samples keep their previous labels.
Adding Juju labels also creates new series/streams; earlier history retains the
previous label sets.

## Logging and the management listener

`log-level` controls generated Alloy logging: `debug`, `info`, `warn` or `error`.
The earlier `warning` and `critical` values remain accepted as aliases for
`warn` and `error`. Invalid settings block the candidate while retaining the
running configuration. Logging changes use reload rather than service restart.
See [Alloy's logging reference](https://grafana.com/docs/alloy/latest/reference/config-blocks/logging/).

Fresh deployments default to `--server.http.listen-addr=127.0.0.1:6987`.
Explicit `custom_args` are honored. Change that setting deliberately if remote
management is required. Changing runtime arguments requires a service restart,
which the charm signals in its log and status; changing the listener default
can therefore restart an older deployment during normal configuration
reconciliation. `upgrade-charm` itself preserves the running service.

## Failure and buffering behavior

| Situation | Behavior |
| --- | --- |
| No remote-write destination | Network components and secret files are withdrawn; unit waits; other pipelines remain available |
| Unreachable device or rejected device credentials | Scrape fails; inventory stays unchanged |
| Unknown profile or missing/invalid primary IPv4 | Device is excluded |
| Discovery refresh fails | Running Alloy retains previous targets; discovery failure counter increases |
| Successful empty discovery response | Previously discovered devices are withdrawn without restarting Alloy |
| Cold start during discovery outage | No persisted inventory snapshot; no known targets until a successful refresh |
| Invalid new settings or credential YAML | Unit blocks and retains the previous applied generation |
| Lost secret grant or missing required secret reference | Network collection is withdrawn, credentials retired, unit blocks; if withdrawal cannot apply, the collector is stopped |
| Config validation or reload fails | Applied state stays unchanged; previous files and service settings are restored; next event retries |
| Network monitoring disabled | Network components and credentials are withdrawn after a successful apply |
| `config-override` set | Automatic discovery/secret management is bypassed; the override owns its configuration; existing managed files explicitly referenced by it are retained |

When network collection is withdrawn while enabled, local self metrics retain
the last applied collector label. This keeps collector-health alerts scoped to
the still-running service while the missing-target alert reports withdrawal.

Discovery refresh defaults to `60s`, device scraping to `30s`, and timeout to
`15s`. Use `netbox-refresh-interval`, `snmp-scrape-interval` and
`snmp-scrape-timeout` to tune them; timeout must be shorter than the interval.

The remote-write component remains `prometheus.remote_write.metrics`, preserving
its WAL identity. Service-default updates preserve the package's `STORAGE_PATH`
and any explicit storage arguments. The existing maximum WAL keepalive remains
30 minutes; extended outages can lose unsent data. Size the persistent filesystem
and verify delivery through your existing authenticated Mimir ingress.

The charm adds collector-scoped rules for failed scrapes, no eligible targets,
discovery failures, missing collector self metrics, remote-write backlog and low
disk space using the existing `alert_rules` transport. The downstream ruler must
already accept and evaluate that transport. Collector-loss alerts need backend
evaluation because an unreachable collector cannot send its own health signal.

Self scraping follows the configured management listener and the debugging
listener when debugging is enabled.

## Troubleshoot missing SNMP metrics

Check `up{job="snmp/network"}` in the metrics backend, then
`{job="snmp/network", __name__=~"ifHC.*"}` for interface counters. A healthy
Alloy component or active Juju unit alone does not prove discovery or successful
SNMP collection.

- **No SNMP targets:** check discovery errors in the Alloy service journal,
  DNS resolution from the collector machine, NetBox certificate trust, filters,
  primary IPv4 and the device's `snmp_auth` profile. Workstation hostname
  resolution does not configure the collector's resolver.
- **Secret validation error:** NetBox requires `token`; SNMP profiles require
  `snmp-yaml`; the CA bundle requires `ca-pem`. Check field names and grants
  without copying credentials into logs or config.
- **Targets with `up=0`:** confirm device reachability and SNMPv3 user, security
  level, authentication/privacy protocols and passphrases. Authentication digest
  errors indicate a mismatch; compare the device settings with its selected
  profile.
- **`up=1` but missing counters:** check `scrape_samples_scraped` and the device's
  support for the embedded `system,if_mib` modules. Keep device/interface labels
  when querying the returned counters.

## Release and acceptance

Fresh installations pin Alloy `1.20.1-1`; local fixture tests use Alloy `1.20.1`.
Charm refresh continues to skip workload installation, rewriting and restarting
in `upgrade-charm`. Existing units must have an Alloy version supporting the
rendered components before enabling network monitoring. A subsequent normal
configuration or credential event applies changed settings.

Plugin installation, NetBox/plugin versions, real OPNsense/Cisco SNMP behavior,
Mimir ingestion, persistent service storage and live charm refresh require a
nonproduction acceptance run. See the
[verification instructions and current scope](build-test-deploy.md).
Interface enrichment, custom vendor modules, HA and persistent discovery
snapshots remain subsequent milestones.
