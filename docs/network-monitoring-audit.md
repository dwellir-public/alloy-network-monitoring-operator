# NetBox network monitoring: audit and implementation plan

The architecture in `PLAN.md` is suitable for this charm. Extend the existing
configuration builder and workload helpers, and rename the charm to
`alloy-network-monitoring`. Keep recurring inventory discovery inside Alloy.
The first release should deliver NetBox discovery, SNMPv3 collection, device
identity, and delivery to Mimir. Interface enrichment and collector HA remain
later milestones.

This is a bounded readiness audit and change plan, not a full charm compliance
assessment. Source inspected: `237aab5e99f6b92f8505c0647c9213424105a330`, on
2026-10-09. No applicable `AGENTS.md` was found. `PLAN.md` was the only initial
untracked file; it has not been edited. No charm code, environment, deployment,
release, or Git remote has been changed. Provenance is recorded in the
[manifest](network-monitoring-audit-evidence/provenance.json); local requirement
excerpts are in [source notes](network-monitoring-audit-evidence/source-notes.md).

The user confirmed the name, config parameters containing Juju secret references
for the NetBox token and SNMPv3 credentials, and **independently optional site,
region, and tag filters**. These instructions supersede the original plan's
mandatory site and `snmp` tag. Retaining existing integrations is the proposed
default; removing them is not part of this plan.

## Before and projected target

Classes distinguish task requirements, external contracts, skill baselines,
maintained recommendations, and design proposals. Applicability is stated in
each class. Targets describe future work; none has been implemented or tested.

| Requirement and source | Class / applicability | Before: observed state | Disposition / task | Projected target | Verification |
| --- | --- | --- | --- | --- | --- |
| Rename and optional filters [R1](network-monitoring-audit-evidence/source-notes.md#task-requirements) | task-required / network capability | GAP: metadata, package, workflow artifacts and tests use `alloy-vm`; no NetBox inputs [E1](#e1-packaging-and-naming) | Implement P1, P2 | New name; each empty filter omitted | Name/artifact consistency; all filter combinations |
| NetBox token and SNMPv3 secrets [R1](network-monitoring-audit-evidence/source-notes.md#task-requirements) | task-required / network capability | GAP: no secret lifecycle observers or network credential files [E2](#e2-configuration-and-secret-lifecycle) | Implement P2 | Secret references in config; protected files; rotations reconcile | Missing/granted/changed/removed secrets; file modes; no credential bodies in state/logs |
| Native discovery and SNMP pipeline [R2](network-monitoring-audit-evidence/source-notes.md#plan-requirements) | task-required / network capability | GAP: builder supports HTTP metrics scrapes, not discovery or SNMP [E3](#e3-metrics-builder-and-destinations) | Implement P3 | Alloy discovers and polls without hook polling | Real Alloy fixture tests and lab SNMP queries |
| Device labels and strict profile selection [R2](network-monitoring-audit-evidence/source-notes.md#plan-requirements) | task-required / network capability | GAP: no network identity contract [E3](#e3-metrics-builder-and-destinations) | Implement P3 | Stable device ID, actual site, selected collector; no credential fallback | Rename device; missing address/profile; final exporter targets |
| Semantic validation and successful apply [R2](network-monitoring-audit-evidence/source-notes.md#plan-requirements) | task-required / config lifecycle | PARTIAL: temporary candidate and atomic file replacement exist; check uses `fmt`, and applied state advances before reload [E2](#e2-configuration-and-secret-lifecycle) | Implement P1, P2 | `alloy validate`; successful reload advances applied state; failure recovery is explicit | Invalid component; invalid SNMP YAML; reload failure and subsequent retry |
| Mimir URL/authentication/tenant/CA [R2](network-monitoring-audit-evidence/source-notes.md#plan-requirements) | task-required / destination capability | PARTIAL: plain relation yields URL only; cloud path has basic auth/CA; no tenant header or external URL config [E3](#e3-metrics-builder-and-destinations) | Preserve integration; resolve destination in P0; implement missing path in P2 if needed | Deployment contract supported for selected Mimir ingress | Real ingestion using selected auth, tenant and trust settings |
| WAL continuity and bounded buffering [R2](network-monitoring-audit-evidence/source-notes.md#plan-requirements) | task-required / runtime state | PARTIAL: stable `metrics` writer, `30m` max keepalive; actual storage path/continuity unverified [E3](#e3-metrics-builder-and-destinations) | Preserve component identity; document/verify P1, P4 | Persistent package storage; measured buffering policy | Mimir outage, process restart and charm refresh; disk/backlog checks |
| Source ownership [S3](network-monitoring-audit-evidence/source-notes.md#skill-baselines) | required-baseline / existing machine observability; task-required / new network labels | UNKNOWN: existing source-specific rendering present; no fresh backend evidence [E5](#e5-existing-observability-contracts) | Preserve existing contracts; verify P4 | Existing workload ownership retained; network device metrics bypass collector topology injection | Regression suite and fresh backend labels |
| Packaging [S1](network-monitoring-audit-evidence/source-notes.md#skill-baselines) | required-baseline / shared charm | PASS, static: consolidated definitions, tracked lock, uv plugin, explicit ops/testing deps [E1](#e1-packaging-and-naming) | Preserve P1 | Same packaging contract under new name | Locked dependency and packed artifact checks on each declared base |
| Real integration coverage [S2](network-monitoring-audit-evidence/source-notes.md#skill-baselines) | required-baseline / shared charm | GAP: pytest-operator deployment-only integration test; no Jubilant behavior coverage [E4](#e4-tests-and-ci) | Implement P4 | Local built artifact tested with pytest/Jubilant and real Alloy | Discovery, secrets, backend and refresh scenarios per supported base |
| Charm refresh safety [S4](network-monitoring-audit-evidence/source-notes.md#skill-baselines) | required-baseline / shared charm | PARTIAL: hook avoids restart/config rewrite; documentation lacks explicit upgrade distinction; live continuity unknown [E2](#e2-configuration-and-secret-lifecycle), [E6](#e6-documentation) | Preserve hook; document and verify P1, P4 | Refresh preserves running workload and storage; later reconciliation activates changed rendering | Event assertions plus PID/start-time and telemetry before/after refresh |
| Loopback management listener [R2](network-monitoring-audit-evidence/source-notes.md#plan-requirements) | agent-proposed / adopting plan's narrow exposure | GAP to proposal: default binds `0.0.0.0:6987`; debugging switches port while self-scrape stays at 6987 [E7](#e7-management-interface) | Implement P1 | Loopback by default; self-scrape follows effective management address | Generated args and live self-metrics in normal/debug modes |
| Reproducible workload and documentation [R2](network-monitoring-audit-evidence/source-notes.md#plan-requirements), [S5](network-monitoring-audit-evidence/source-notes.md#documentation-recommendations) | task-required / tested versions; recommended-default / docs; agent-proposed / package pin | GAP: installs unpinned APT Alloy; docs contain workstation paths [E6](#e6-documentation), [E8](#e8-workload-version) | Implement P1, P4 | Tested versions recorded; tested package selected; portable operator/developer docs | Version record matches binary; doc links; build/test instructions |
| CI action provenance [E4](#e4-tests-and-ci) | agent-proposed / inherited maintenance | GAP: two unpinned actions; policy regex misses inline `- uses:` declarations | Implement P1 | All actions pinned; check traverses parsed workflow steps | Existing policy tests with both YAML declaration styles |

The implementation adds the network capability while preserving deployment and
integration contracts. Production runtime evidence remains unavailable in this
audit. Blockchain-specific action/config/metadata standards do not apply to this
collector. Full machine-observability schema, downstream rule consumers,
dashboards, release readiness and unrelated charm features were not exhaustively
assessed.

## Changes to make to the original design

**Optional filters change enrollment.** Use no implicit site or `snmp` tag.
When filters are empty, all visible devices satisfying the remaining filters
are eligible, subject to address and SNMP profile validation. A collector can
cover several sites; do not derive its identity from one site. Preserve each
device's actual `site` label. Keep `region` as a discovery filter: the inspected
plugin does not emit region metadata, so it cannot establish an actual per-device
region label. NetBox's device filter follows `site__region`; verify descendant
region behavior against the installed release. Multiple `tag=` values use AND.
Use repeated query parameters and proper URL encoding, not a comma-joined API
value. [NetBox filters](https://netbox.readthedocs.io/en/stable/reference/filtering/),
[device filter implementation](https://raw.githubusercontent.com/netbox-community/netbox/main/netbox/dcim/filtersets.py).

**Confirm plugin availability before promising automatic enrollment.** Its
documented compatibility is plugin 2.x with NetBox 4.x; that is not evidence of
compatibility with the user's installation. A manually generated discovery file
does not meet the automatic-enrollment acceptance criterion. If the plugin
cannot be installed, decide separately whether to accept manual discovery or
design an automatically refreshed adapter. The latter expands v0.1 scope.
[Plugin documentation](https://raw.githubusercontent.com/FlxPeters/netbox-plugin-prometheus-sd/main/README.md).

**Make the actual target transformation explicit.** Plugin targets start with
device names. Select `__meta_netbox_primary_ip4`; the inspected plugin strips
CIDR prefixes. Map only approved labels, then remove residual discovery/internal
labels before passing targets to the exporter. Set `name`, `address`, `module`
and `auth`; reject absent identity/address and profiles missing from the local
validated secret. Recheck site/status/tags locally when configured. Region
selection depends on NetBox's filtering because the plugin supplies no region
label. Test duplicated device entries from plugin config contexts.
[Plugin utilities](https://raw.githubusercontent.com/FlxPeters/netbox-plugin-prometheus-sd/39eb655aeba85d20ffc7c4551324fe4ec5d875fc/netbox_prometheus_sd/api/utils.py),
[serializer](https://raw.githubusercontent.com/FlxPeters/netbox-plugin-prometheus-sd/39eb655aeba85d20ffc7c4551324fe4ec5d875fc/netbox_prometheus_sd/api/serializers.py).

After export, set `job="snmp/network"` and
`instance="netbox-device-<id>"`. Keep exporter-generated addresses and query
parameters intact. Explicitly allow the device labels required by the plan;
preserve SNMP interface observations. Use `config_merge_strategy="merge"` and
retain collection diagnostics. Use `local.file` with `is_secret=true` for
the SNMP YAML, which the exporter accepts through its secret-valued `config`
argument. Verify reload/rotation with the selected binary.
[Alloy SNMP component](https://grafana.com/docs/alloy/latest/reference/components/prometheus/prometheus.exporter.snmp/),
[exporter implementation](https://raw.githubusercontent.com/grafana/alloy/2cdeba9a3cdace81abe907a5ec1af137cb577815/internal/component/prometheus/exporter/snmp/snmp.go).

**NetBox token versions affect the header.** Legacy v1 tokens use `Token`;
v2 tokens use `Bearer` with the full `nbt_<key>.<token>` credential. Support an
explicit authorization scheme instead of assuming bearer or legacy token auth.
Keep writes disabled and use the separate monitoring token.
[NetBox authentication](https://netbox.readthedocs.io/en/stable/integrations/rest-api/#authenticating-to-the-api).

**Reuse of remote write has limits.** A plain Juju integration is sufficient
only if the supplied ingress requires no collector-side auth/tenant/CA settings,
or a proxy provides them. Extend the endpoint model where the selected deployment
requires more. Preserve the relation schema; do not invent fields in a vendored
library. An external Mimir requires a configured URL and secret references.
The current writer fans out to all attached sinks, including Grafana Cloud;
document that behavior when configuring network telemetry. Remote write alone
does not provision alert rules to an external Mimir.
[Mimir API](https://grafana.com/docs/mimir/latest/references/http-api/),
[authentication](https://grafana.com/docs/mimir/latest/manage/secure/authentication-and-authorization/).

**The configuration transaction must include secrets and service defaults.**
Stage and validate candidate YAML/files/config, atomically promote them with
explicit recovery, then reload. Update applied state only after success.
The current `.bak` is overwritten with the candidate, so it is not a rollback
copy of the previous applied generation. Preserve unrelated keys in
`/etc/default/alloy`; the existing charm truncates that file to two settings.
Use protected, explicit file modes; the existing atomic writer does not set
secret-specific permissions. Store secret references and revision information
instead of credential bodies in `StoredState`.
[Validation scope](https://grafana.com/docs/alloy/latest/reference/cli/validate/).

## Proposed configuration contract

| Config parameter | Default / requirement | Behavior |
| --- | --- | --- |
| `network-monitoring-enabled` | `false` | Explicitly enables generated network pipeline |
| `netbox-url` | Empty; required when enabled | NetBox base URL; charm builds the plugin devices URL |
| `netbox-token-secret-id` | Empty; required when enabled | Granted Juju secret containing `token` |
| `netbox-auth-scheme` | `Bearer`, with `Token` supported | Non-secret authorization scheme for the selected token version |
| `snmp-auth-secret-id` | Empty; required when enabled | Granted Juju secret containing `snmp-yaml` |
| `netbox-site` | Empty | Optional site slug filter; empty omits it |
| `netbox-region` | Empty | Optional region slug filter; empty omits it |
| `netbox-tags` | Empty | Optional comma-separated tag slugs; emit repeated `tag=` parameters, matching all |
| `netbox-status` | `active` | Proposed default; empty also omits this filter |
| `collector-id` | Empty; required when enabled | Stable monitoring-scope name independent of filters |
| `netbox-refresh-interval` | `60s` | Inventory refresh, independent of scraping |
| `snmp-scrape-interval` | `30s` | Initial test setting |
| `snmp-scrape-timeout` | `15s` | Initial test setting; must be less than scrape interval |

The new parameter names and defaults beyond the user's requested secret
references and optional filters are design proposals. Add a CA secret reference
for NetBox if private trust is required. Destination parameters depend on P0;
do not overload `grafana-cloud-config` to represent arbitrary Mimir credentials.

The `snmp-yaml` secret contains an `auths` mapping in the selected embedded
snmp_exporter version's schema. Initially allow profiles named `opnsense` and
`cisco`, only if present and valid locally. Require SNMP version 3; recommend
`authPriv`. Validate usernames, security levels and matching authentication and
privacy settings; do not accept custom modules through this secret in v0.1.
Write the resulting file to `/etc/alloy/snmp.yaml`, readable by Alloy with an
explicit mode such as `0640 root:alloy`. Protect the token file similarly.

Observe relevant secret changes and reconcile the latest granted revision.
Malformed replacement content should block the change and retain the last
successfully applied generation. Explicit removal of a secret reference or loss
of access should disable the affected network path, remove its credential files,
and report the missing dependency. Test this distinction. Inventory updates need
no hook or reload. A full `config-override` continues to supersede generated
configuration; document that managed network configuration is inactive in that
mode and make the status understandable.

## Implementation sequence

### P0: resolve runtime inputs

Confirm NetBox version, plugin installation permission/version and a redacted
actual discovery response; selected Mimir ingress and auth/tenant/CA contract;
and a nonproduction lab with one OPNsense device. Record versions as selected or
tested accurately. These inputs gate live validation, not the local renderer,
secret lifecycle work or rename. Use one active collector, even if its optional
filters cover several sites.

### P1: rename and strengthen the inherited foundation

Update `charmcraft.yaml`, `pyproject.toml`, regenerate the root package entry in
`uv.lock`, and update artifact paths in both release/provenance workflows.
Adapt tests asserting charm-owned identity and artifact names. Preserve vendored
library bytes, their historical provenance URLs, relation endpoint names,
`alloy-peers`, the existing rule-cache key, `/etc/alloy`, the `alloy` systemd
service, and `prometheus.remote_write "metrics"`.

In `src/alloy.py` and `src/charm.py`, replace the verification command with
`alloy validate`, preserve service-default keys, advance applied state after
reload success, and support recovery/retry after failure. Set a loopback default
and align self-scraping with effective runtime args. Pin a tested Alloy package
for reproducible deployment without automatically upgrading it on charm refresh.
Fix action pinning and the policy test's YAML parsing blind spot.

Update `README.md`, add `ARCHITECTURE.md` and `DEVELOPING.md`, and replace the
current architecture doc with a pointer when migrated. Leave historical plans
unchanged. Registration/publication of the new Charmhub name and any repository
rename are separate future publication tasks. Do not treat a new name as a
verified in-place migration for deployed `alloy-vm` applications.

### P2: validated settings, secrets and sink configuration

Add `src/network_monitoring.py` for typed settings, validation and discovery URL
assembly. Add `src/secret_files.py` for protected staging/promotion/recovery.
Keep event wiring and Juju secret resolution in `src/charm.py`. Add config keys
and secret event handling, integrate no-upstream waiting behavior, and reconcile
the override/disabled modes. Extend `src/outbound_endpoints.py` and rendering
only for destination features established in P0; include them in endpoint
deduplication. Validate duration relationships and escape rendered strings.

### P3: one production renderer, tested with Alloy

Add network block rendering to the helper and compose it from
`src/config_builder.py`. The current project uses Python rendering; introducing
Jinja solely for the proposed template is unnecessary. Test this exact renderer
with a static SNMP target first, then HTTP discovery fixtures. Keep any standalone
lab configuration generated from the same renderer rather than maintaining a
second production implementation. Add fixtures for optional filter combinations,
multiple sites, missing fields, unknown profiles, duplicate entries and emptiness.

### P4: live behavior, alert delivery and release evidence

Use pytest/Jubilant with a built local artifact and an explicitly selected
nonproduction model. Add binary-level fixture tests independently of live Juju
tests. Inspect cleanup before execution; never use the current model implicitly.
Test each retained supported Ubuntu base and record artifact hash and actual
workload version. Run lint/static/unit/provenance gates and build checks, followed
by the behavior checks below.

Ship `alerts/network.rules.yaml` for discovery failures, device failures,
disappeared targets, delivery backlog/disk pressure and collector availability.
Compose owned network rules with the existing principal-rule aggregation in
`src/charm.py`/`src/alert_rules.py`, preserving bounds, encoding negotiation and
withdrawal. Label collector self-metrics with the monitoring scope for selectors;
do not add collector Juju ownership to device counters. Verify rules reach the
selected backend. If Mimir is external, document and test its separate ruler
provisioning path. A total forwarding outage also needs a monitor outside the
affected collector's delivery path. Do not claim independent detection from
self-metrics that cannot reach Mimir.

Treat the one-active-collector policy as a deployment constraint: document
target ownership across applications and reject multiple units in one enabled
network-monitoring application until HA or sharding is implemented.

Interface metadata, custom MIB generation, traps, persistent discovery snapshots,
HA/sharding and multi-NetBox identity remain deferred. The current stable device
ID assumes one NetBox inventory; future multiple inventories need namespacing.

## Acceptance and evidence still needed

| Scenario | Required observation |
| --- | --- |
| No site/region/tags; each singly; combinations | Only configured query parameters present; zero filters accepted; region follows installed API semantics; multiple tags match all |
| One OPNsense, then Cisco | Real interface metrics and diagnostics queryable in intended Mimir tenant |
| Enrollment/change/removal under configured filters | Targets change at discovery refresh without charm polling, config edits or Alloy restart |
| Wrong site/status/tag; missing address/identity; unknown or unavailable auth profile | Ineligible target not scraped; no public/default credentials |
| Device rename or address change | Stable instance/NetBox ID; updated name/address; original interface observations retained |
| Exporter-generated targets | Correct final job/instance; internal routing and query parameters preserved; no stray inventory labels |
| Invalid JSON/content type, HTTP errors, successful empty list | Running process retains targets on failed refresh; successful empty list removes them; distinct alerts |
| Cold start during NetBox outage | No assumed persisted discovery cache; behavior documented and collector/discovery failure observable |
| Credential grant/rotation/malformed revision/removal | Protected files and status follow the stated policy; unrelated telemetry retained; no secret leakage |
| Invalid Alloy config or failed reload | Previous applied generation identifiable/recoverable; no false successful apply; later event retries |
| No remote-write destination vs endpoint outage | Missing destination disables network collection and waits; configured outage exercises bounded WAL |
| Restart and charm refresh during buffered delivery | Stable storage/writer identity; refresh preserves process; backlog recovery and loss limits measured |
| Empty target set and collector loss | Safeguards work even when device `up` series are absent; independent monitoring covers pipeline loss |
| Existing integrations and alerts | Previous source ownership, syslog/log behavior, sink fan-out and rule publication remain correct |

No unit, build, binary integration or live tests were executed in this audit.
`pytest`, `ops`, an existing project virtual environment and the Alloy binary
were not available locally; dependencies were not installed. Static inspection
and public-source verification establish the findings above, not live delivery.
The skill's audit-rule consistency scanner passed; that is not a charm test.

## Evidence index

### E1: packaging and naming

[Charm definitions](https://raw.githubusercontent.com/dwellir-public/alloy-vm-operator/237aab5e99f6b92f8505c0647c9213424105a330/charmcraft.yaml),
[dependency definitions](https://raw.githubusercontent.com/dwellir-public/alloy-vm-operator/237aab5e99f6b92f8505c0647c9213424105a330/pyproject.toml),
[tracked lock](https://raw.githubusercontent.com/dwellir-public/alloy-vm-operator/237aab5e99f6b92f8505c0647c9213424105a330/uv.lock).
Metadata and root package use `alloy-vm`. Both Ubuntu 22.04/24.04 amd64 platforms
are declared. Static BUILD-001 criteria are satisfied.

### E2: configuration and secret lifecycle

[Charm orchestration](https://raw.githubusercontent.com/dwellir-public/alloy-vm-operator/237aab5e99f6b92f8505c0647c9213424105a330/src/charm.py),
[workload helpers](https://raw.githubusercontent.com/dwellir-public/alloy-vm-operator/237aab5e99f6b92f8505c0647c9213424105a330/src/alloy.py).
Relevant symbols: `__init__`, `_configure`, `_validate_config_text`,
`_write_alloy_systemd_unit_defaults`, `_on_upgrade_charm`, `verify_config`,
`write_config_text`, `_write_file_atomic`. Candidate validation precedes file
replacement, but `fmt` is used and `last_good_config` advances before reload.
No network secret event handlers exist. Helpers recursively give Alloy ownership
of its config directory; a secret helper must enforce its own ownership/modes.

### E3: metrics builder and destinations

[Builder](https://raw.githubusercontent.com/dwellir-public/alloy-vm-operator/237aab5e99f6b92f8505c0647c9213424105a330/src/config_builder.py),
[sink model](https://raw.githubusercontent.com/dwellir-public/alloy-vm-operator/237aab5e99f6b92f8505c0647c9213424105a330/src/outbound_endpoints.py),
[remote-write consumer](https://raw.githubusercontent.com/dwellir-public/alloy-vm-operator/237aab5e99f6b92f8505c0647c9213424105a330/lib/charms/prometheus_k8s/v1/prometheus_remote_write.py).
Relevant symbols: `_render_remote_write`, `_render_endpoint_block`,
`_remote_write_endpoint_urls`, `_active_metrics_scrape_jobs`, `endpoints`.
No NetBox/SNMP components; plain relation yields URL only. WAL uses stable
component name `metrics` and max keepalive `30m`; storage depends on runtime
package/service configuration. [WAL documentation](https://grafana.com/docs/alloy/latest/reference/components/prometheus/prometheus.remote_write/).

### E4: tests and CI

[Integration test](https://raw.githubusercontent.com/dwellir-public/alloy-vm-operator/237aab5e99f6b92f8505c0647c9213424105a330/tests/integration/test_charm.py),
[policy tests](https://raw.githubusercontent.com/dwellir-public/alloy-vm-operator/237aab5e99f6b92f8505c0647c9213424105a330/tests/unit/test_workflow_policy.py),
[release workflow](https://raw.githubusercontent.com/dwellir-public/alloy-vm-operator/237aab5e99f6b92f8505c0647c9213424105a330/.github/workflows/release.yml),
[provenance workflow](https://raw.githubusercontent.com/dwellir-public/alloy-vm-operator/237aab5e99f6b92f8505c0647c9213424105a330/.github/workflows/dependency-provenance.yml).
Integration coverage only builds/deploys/waits. The provenance workflow uses
`actions/checkout@v6` and `actions/upload-artifact@v4`; the existing policy regex
matches zero action declarations in that file because all are inline `- uses:`.
This is a policy-check blind spot, not evidence that the current suite failed.

### E5: existing observability contracts

[Source translation](https://raw.githubusercontent.com/dwellir-public/alloy-vm-operator/237aab5e99f6b92f8505c0647c9213424105a330/src/machine_observability_sources.py),
[rule helpers](https://raw.githubusercontent.com/dwellir-public/alloy-vm-operator/237aab5e99f6b92f8505c0647c9213424105a330/src/alert_rules.py),
[vendored provenance](https://raw.githubusercontent.com/dwellir-public/alloy-vm-operator/237aab5e99f6b92f8505c0647c9213424105a330/dependencies/vendored.json).
Existing principal-specific telemetry and rule aggregation are reusable.
No authoritative-schema equivalence or fresh backend claim is made here.

### E6: documentation

[Architecture](https://raw.githubusercontent.com/dwellir-public/alloy-vm-operator/237aab5e99f6b92f8505c0647c9213424105a330/docs/charm-architecture.md),
[development notes](https://raw.githubusercontent.com/dwellir-public/alloy-vm-operator/237aab5e99f6b92f8505c0647c9213424105a330/docs/build-test-deploy.md).
Current docs contain workstation-specific paths; canonical root architecture
and developer docs are absent. No unrelated documentation rewrite is proposed.

### E7: management interface

`custom_args` in [charm definitions](https://raw.githubusercontent.com/dwellir-public/alloy-vm-operator/237aab5e99f6b92f8505c0647c9213424105a330/charmcraft.yaml)
defaults to `0.0.0.0:6987`; `_apply_live_debugging` in E2 changes this to port
12345, while `_render_local_metrics_relabel` in E3 hard-codes port 6987.
The source demonstrates the mismatch; live consequences were not tested.

### E8: workload version

`install()` in E2 executes `apt-get install -y alloy` without a package version.
Unit-test version strings are parsing fixtures, not evidence of the deployed or
tested Alloy version. Upstream source commits in the manifest are research
references, not a certified runtime combination.

## Remaining inputs

The name and optional filters are settled. NetBox version/plugin availability,
Mimir ingress contract, and the nonproduction validation environment are still
needed for the dependent live checks. The listed implementation tasks otherwise
have concrete defaults and need no further design choice to begin local work.
