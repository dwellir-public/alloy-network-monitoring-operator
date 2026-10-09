# Source notes for the network-monitoring audit

These are bounded excerpts and conversation requirements, not a replacement
for the source plan or skill. Original file hashes are in the adjacent manifest.

## Task requirements

R1 records the user's requested changes, received during this audit:

- Use the charm name `alloy-network-monitoring`.
- Expose Juju config parameters referencing Juju secrets for the NetBox token
  and SNMPv3 authentication credentials.
- Support NetBox region, tag and site filters; all three are optional.

The original request asks for an audit and how to proceed. Implementation,
deployment and publication have not been executed.

## Plan requirements

R2 consists of selected original `PLAN.md` excerpts; later task requirements
above override conflicting assumptions in the original document.

### PLAN.md lines 18-22

```text
The first monitoring release should achieve one clear result:

> **Enroll a device in NetBox, and the appropriate site’s Alloy collector starts monitoring it without editing a target file or restarting Alloy.**

Interface-description enrichment should be the next milestone, not a prerequisite for getting discovery working.
```

### PLAN.md lines 269-288

```text
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
```

### PLAN.md lines 310-325

```text
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
```

### PLAN.md lines 368-378

```text
Keep authentication profiles in a workload-readable secret file:

```text
/etc/alloy/snmp.yaml
```

NetBox contains only the profile name. Each site can resolve that name to its own credentials.

Continue using the merge strategy for adding local authentication profiles to the embedded configuration. The exporter supports dynamically supplied targets and locally provided authentication configuration. [Grafana Labs](https://grafana.com/docs/alloy/latest/reference/components/prometheus/prometheus.exporter.snmp/)

For the standard modules, do not introduce a MIB-generator workflow yet. Vendor MIBs and generated custom modules belong in a later, version-controlled collection-profile milestone. The upstream exporter distinguishes its ready-made configuration from configuration generated for custom walks and vendor MIBs. [GitHub](https://github.com/prometheus/snmp_exporter)
```

### PLAN.md lines 406-423

```text
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
```

### PLAN.md lines 498-513

```text
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
```

### PLAN.md lines 581-602

```text
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
```

### PLAN.md lines 615-638

```text
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
```

## Skill baselines

### S1: references/generic/standards.md

```text
### BUILD-001 — Packaging and dependency rules

Authority: **required-baseline**. Only the dependency ownership, tracked lock, uv plugin, consolidated definitions and explicit ops/runtime-testing dependencies in the packaging section are required by BUILD-001.


This file defines the shared machine-charm baseline for Dwellir.
```

### S2: references/generic/testing.md

```text
### DEPLOY-001 — Built artifact and integration behavior

Authority: **required-baseline**. The integration section requires pytest/Jubilant, deployment of a built local artifact, and user-visible relation/config behavior coverage. Temporary models remain recommended.


Use this file when changing tests, validation workflows, or CI-aligned local commands.
```

### S3: references/juju-topology.md

```text
### OBS-001 — Source-to-backend identity

Authority: **required-baseline**. Accurate source ownership and the applicable identity mappings are required from payload through fresh backend telemetry, against the pinned authority. Full schema/artifact conformance and dashboard/alert selection checks are recommended here unless separately required by the selected external contract.


This is the sole normative topology reference in this skill. Other references link here for identity fields, labels, authority and library selection. Applicability: workload-source publication, collector relabeling and topology selectors in metrics, logs, dashboards and alerts. Non-topology interface and artifact requirements remain in generic observability.
```

### S4: references/generic/juju-concepts.md

```text
## Upgrade-Charm Behavior

- Treat `upgrade-charm` as a real lifecycle surface, not an afterthought.
- Preserve existing behavior unless the task explicitly changes upgrade flow.
- Ensure charm upgrades do not silently break integration handling, config rendering, or workload management.
- Document charm-upgrade and workload-upgrade behavior in `ARCHITECTURE.md`.
- Ensure that `upgrade-charm` does not restart workloads.
- Signal workload restart needs after `upgrade-charm` via logs and charm status if a later operator step is required.
```

## Documentation recommendations

S5: `references/generic/documentation.md` classifies its charm documentation
guidance as recommended-default and identifies `ARCHITECTURE.md` and
`DEVELOPING.md` as the canonical architecture and developer documentation.
Its portable-path instructions independently require repository-relative links
or concrete verified web URLs in generated documents.

## Static verification observations

The existing workflow-policy regex matches zero action declarations in
`.github/workflows/dependency-provenance.yml`. A pattern recognizing both
`uses:` and `- uses:` finds three: checkout at `v6`, setup-uv at an immutable
commit, and upload-artifact at `v4`. This proves the regex blind spot and two
unpinned declarations; the unit test suite was not run.

The remotely fetched charmcraft definition at the audited commit byte-matches
the local tracked file. `git ls-remote` identifies that commit as origin/main
and identifies the plugin and Alloy research commits in the manifest. Pinned
plugin utilities/serializers and Alloy SNMP source were retrieved successfully.

The skill's rule scanner reported matching source IDs, authorities and catalog
coverage. No charm compliance or runtime result follows from that check.
