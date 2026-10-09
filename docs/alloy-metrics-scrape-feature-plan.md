# alloy-vm Metrics Scrape Feature Plan

This document plans a separate Alloy feature area: scraping metrics from multiple related workloads over `prometheus_scrape` and forwarding those metrics to an upstream metrics backend such as `mimir-vm` over `prometheus_remote_write`.

It is intentionally planning-only. No implementation decisions in this document are applied to the charm yet.

## Goal

Support a deployment where:

- one Alloy unit can scrape metrics from multiple remote units and applications
- the scraped metrics are forwarded upstream to `mimir-vm`
- Juju topology from the scraped source is preserved on the resulting metrics
- cross-model relations preserve the remote source topology rather than the local Alloy topology
- the charm does not accumulate unbounded local metrics backlog when no remote write destination exists

Example intended end state:

- `dummychain` provides `prometheus_scrape`
- `loki-vm` optionally provides `prometheus_scrape`
- `alloy` requires those scrape targets
- `alloy` requires `prometheus_remote_write` from `mimir-vm`
- Alloy scrapes all discovered targets and forwards them to Mimir with source `juju_*` labels preserved

## Current Charm State

The current Alloy charm already has a small local metrics baseline, but no relation-driven metrics support:

- `src/config_builder.py` renders:
  - `prometheus.exporter.unix "default"`
  - `prometheus.scrape "default"`
  - `forward_to = []`
- this means Alloy currently self-scrapes local exporter and self metrics, but drops them because no metrics sink is configured
- `charmcraft.yaml` currently exposes only:
  - `send-loki-logs`
  - `syslog-receiver`
- there is no `prometheus_scrape` or `prometheus_remote_write` relation metadata
- there are no vendored metrics relation libraries under `alloy-vm-operator/lib`
- the current charm has no peer relation and no metrics unit coordination logic

So the feature is feasible from the current shape of the charm, but it is not partially implemented yet apart from the local self-scrape stanza.

## Feasibility Summary

This is feasible with the current Juju relation libraries and the existing Alloy architecture.

The two key building blocks already exist in this workspace:

- `MetricsEndpointConsumer` from `charms.prometheus_k8s.v0.prometheus_scrape`
- `PrometheusRemoteWriteConsumer` from `charms.prometheus_k8s.v1.prometheus_remote_write`

The standard `prometheus_scrape` consumer library already expands scrape jobs from related applications and injects Juju topology labels from provider-side `scrape_metadata`. In the normal wildcard-target case, it also preserves `juju_unit`.

That means Alloy does not need to invent its own topology model. The main implementation task is translating the consumer library’s generated scrape jobs into Alloy configuration blocks and only enabling them when the upstream remote write path is available.

## Release-1 Behavior Proposal

Release 1 should optimize for correctness and low operational surprise, not HA scrape distribution.

### Topology and unit behavior

- A single Alloy unit must be able to scrape many related targets.
- Alloy is expected to run as a single unit for this feature area.
- We do not plan to implement Alloy clustering or multi-unit scrape coordination.

Reason:

- the intended operational model is one Alloy unit acting as the scraper and forwarder
- avoiding clustering keeps the implementation smaller and avoids duplicate scrape ownership problems
- HA for metrics ingestion is expected to come from the upstream Mimir side, not from a clustered Alloy tier

### Relation names

Use the standard relation names used by the existing libraries unless a strong reason appears to deviate:

- `metrics-endpoint` for required `prometheus_scrape`
- `send-remote-write` for required `prometheus_remote_write`

`mimir-vm` already provides `receive-remote-write` with the same interface, so Juju integration remains valid even though the endpoint names differ.

### Source topology behavior

Metrics scraped from remote workloads should preserve the topology of the scraped source, not the topology of the Alloy unit doing the scrape.

That means the resulting metric series should retain labels such as:

- `juju_model`
- `juju_model_uuid`
- `juju_application`
- `juju_unit`
- `juju_charm`

When the target comes from a cross-model relation, those labels should continue to describe the remote controller/model/application/unit.

Release 1 should not inject local Alloy topology labels into remote-scraped metrics because:

- it changes the source identity semantics
- it increases cardinality
- it complicates queries and alerts

If provenance of the scraper itself is ever needed, that should be a separate later feature with clearly named `scrape_via_*` labels or Alloy self-metrics, not an overload of source topology.

## Strategy When No Remote Write Endpoint Exists

This is the most important operational decision for this feature.

## Recommended behavior

If related scrape targets exist but there is no `prometheus_remote_write` endpoint:

- do not enable relation-derived remote scrape jobs
- do not configure `prometheus.remote_write`
- set a clear status such as:
  - `WaitingStatus("waiting for prometheus_remote_write before enabling related metrics scraping")`

This avoids using local disk as a surprise backlog queue for relation-derived metrics.

## Why this is the right default

The current local self-scrape uses `forward_to = []`, which means Alloy can safely drop metrics rather than persisting them indefinitely. For relation-driven workload metrics, silently scraping and dropping them would waste CPU and network while hiding that nothing is being delivered.

Disabling the relation-derived scrape blocks entirely is better than:

- scraping and dropping everything silently
- scraping into a local WAL with no real upstream configured
- inventing a local spool/retention policy the operator did not ask for

## Behavior when remote write exists but is temporarily unavailable

That is different from having no remote write relation at all.

When a valid remote write destination exists, Alloy’s `prometheus.remote_write` WAL can provide bounded short-term buffering during normal outages.

Release 1 should use a bounded WAL policy and document that:

- short outages are buffered
- long outages eventually drop old samples

This is preferable to indefinite local growth.

## Decided remote write outage buffer policy

Release 1 will use a bounded remote write outage buffer window of `30m`.

That means:

- temporary upstream outages are buffered for up to `30m`
- extended upstream outages beyond `30m` may drop older samples
- the charm should document this clearly as a deliberate storage-safety tradeoff

## Juju Topology Preservation Plan

The preferred plan is to build on the canonical `prometheus_scrape` relation library rather than recreating the relation semantics ourselves.

### How topology arrives

Provider charms using `MetricsEndpointProvider` publish:

- `scrape_metadata` in app relation data
- per-unit address/path data in unit relation data

The consumer library then materializes scrape jobs where:

- wildcard targets are expanded per unit
- `juju_model`, `juju_model_uuid`, and `juju_application` are attached
- `juju_unit` is attached for wildcard-expanded unit targets

### How Alloy should preserve it

The Alloy charm should:

1. Consume jobs from `MetricsEndpointConsumer.jobs()`.
2. Convert each resulting static target into Alloy-compatible target maps.
3. Carry the `juju_*` labels from the job’s static config labels directly into those target maps.
4. Keep those labels untouched through scraping and remote write.

That gives us source-identifying labels in Mimir without inventing a second topology system.

### Important limitation

If a provider charm publishes only fully qualified static targets instead of wildcard unit targets, the consumer library cannot reliably infer `juju_unit` for those targets. In that case the metrics can still preserve app-level topology, but unit-level identity may be absent.

So for release 1, charms we control should publish wildcard targets through the standard provider library whenever possible.

## Cross-Model Relation Behavior

Cross-model scraping is possible if two conditions hold:

1. the `prometheus_scrape` relation data traverses the CMR, which Juju supports
2. the Alloy unit can actually reach the target addresses published by the remote model’s units

Release 1 should treat cross-model support as:

- supported when target networking is routable
- unsupported when the advertised unit addresses are not reachable from the Alloy unit

This is an operational limitation, not a relation-format limitation.

The important design choice is that the metric labels should still reflect the remote source topology, not the local model where Alloy runs.

## Fully Working End-State Example

### `dummychain` -> Alloy -> Mimir

- `dummychain` provides `metrics-endpoint`
- Alloy relates to `dummychain:metrics-endpoint`
- `mimir-vm` provides `receive-remote-write`
- Alloy relates `send-remote-write` to `mimir-vm:receive-remote-write`
- Alloy renders scrape jobs for all dummychain units
- Alloy forwards the samples to the selected Mimir remote write URL
- resulting metrics in Mimir retain `dummychain` source `juju_*` labels

### `loki-vm` -> Alloy -> Mimir

Exactly the same pattern applies, but `loki-vm` first needs to provide `prometheus_scrape`.

### Cross-model target

- remote model application provides `metrics-endpoint`
- local Alloy consumes the CMR
- labels on the resulting metrics identify the remote model/app/unit
- local Alloy topology is not added to those sample labels

## Changes Needed In `alloy-vm-operator`

## Metadata and libraries

- add required relation `metrics-endpoint` using interface `prometheus_scrape`
- add required relation `send-remote-write` using interface `prometheus_remote_write`
- vendor:
  - `charms.prometheus_k8s.v0.prometheus_scrape`
  - `charms.prometheus_k8s.v1.prometheus_remote_write`

## Charm orchestration

- instantiate `MetricsEndpointConsumer`
- instantiate `PrometheusRemoteWriteConsumer`
- observe:
  - scrape targets changed
  - remote write endpoints changed
- trigger config regeneration when either side changes
- add status logic for:
  - scrape targets present but no remote write endpoint
  - invalid translated scrape jobs
  - remote write endpoint relation present but empty

## Config builder

Extend `ConfigBuilder` to accept:

- local-metrics forwarding enablement
- remote scrape jobs produced by the consumer library
- remote write endpoints
- optional bounded WAL settings

Add rendering for:

- `prometheus.remote_write "mimir"` with exactly one endpoint in release 1
- one or more relation-derived `prometheus.scrape` components
- local self metrics forwarding into the same remote write receiver

## Translation layer

Add a translation step from Prometheus job dicts to Alloy config blocks.

Release-1 supported translation subset should be narrow and explicit:

- `job_name`
- `metrics_path`
- `scheme`
- `scrape_interval`
- `scrape_timeout`
- `static_configs`
- static labels from `static_configs`

Release-1 should not try to support every advanced Prometheus scrape field immediately.

If a related charm provides unsupported advanced job fields, Alloy should:

- log a warning
- skip only the unsupported job when possible
- surface clear status if all jobs are unusable

## Local metrics treatment

The current local Alloy/unix scrape should be retained, but improved:

- add local Juju topology labels for Alloy self metrics
- forward local metrics to remote write when available
- avoid blocking local metrics rendering just because relation-derived scrape jobs are absent

## Unit behavior

Release 1 assumes a single Alloy unit does the scraping and forwarding work.

There is no plan in this feature area to:

- distribute scrape jobs across multiple Alloy units
- run Alloy as a scrape cluster
- coordinate failover between Alloy units

## Storage and status behavior

- when no remote write relation exists, disable relation-derived remote scraping entirely
- when remote write exists, enable bounded WAL buffering
- document clearly that long upstream outages can still lose samples

## Tests needed in `alloy-vm-operator`

- unit tests for relation data to Alloy config translation
- unit tests for multiple target units across one relation
- unit tests for multiple relations aggregated into one Alloy config
- unit tests for no-remote-write behavior
- unit tests for topology label preservation
- integration tests:
  - Alloy + dummychain + mimir-vm
  - multiple dummychain units
  - cross-model scrape target if available in test environment

## Changes Needed In Target Charms

## `dummychain`

Today `dummychain` does not expose `prometheus_scrape`.

To make the intended end-to-end flow work, `dummychain` needs:

- a provided `metrics-endpoint` relation
- `MetricsEndpointProvider`
- a real metrics endpoint to publish
- wildcard target publication so unit labels are preserved

## `loki-vm`

Today `loki-vm` also does not expose `prometheus_scrape`.

To be scrapeable by Alloy through the same mechanism, it needs:

- a provided `metrics-endpoint` relation
- `MetricsEndpointProvider`
- published Loki metrics endpoint details

## Other charms

For any charm to participate cleanly in release 1, it should:

- use the standard provider library
- publish wildcard unit targets
- expose routable metrics endpoints

## Decisions To Take Before Implementation

1. Relation names:
   - recommend `metrics-endpoint` and `send-remote-write`

2. Multi-unit Alloy scrape ownership:
   - decided as a single Alloy unit only; no clustered scrape ownership

3. Remote write outage retention:
   - decided as bounded WAL with `max_keepalive_time = "30m"`

4. Unsupported advanced scrape job fields:
   - recommend narrow supported subset, warn and skip unsupported jobs

5. Local Alloy topology on remote-scraped metrics:
   - recommend not adding it in release 1

## Known Limitations

- no Alloy-side scrape HA is planned in this feature area
- if the Alloy unit is down, scraping stops until that unit returns or is manually replaced
- provider charms using non-wildcard targets may lose `juju_unit`
- cross-model scraping still depends on actual IP/DNS reachability between models
- long upstream remote write outages can still lose samples once the configured WAL retention window is exceeded

## Proposed Delivery Phases

## Phase A - Metadata and relation wiring

- [x] Add `prometheus_scrape` metadata on `metrics-endpoint`.
- [x] Add `prometheus_remote_write` metadata on `send-remote-write`.
- [x] Vendor `charms.prometheus_k8s.v0.prometheus_scrape`.
- [x] Vendor `charms.prometheus_k8s.v1.prometheus_remote_write`.
- [x] Instantiate the metrics consumer and remote write consumer in the charm.
- [x] Observe relation change events and trigger config reconciliation.

## Phase B - Config translation

- [x] Translate `MetricsEndpointConsumer.jobs()` output into Alloy scrape blocks.
- [x] Support the release-1 subset of Prometheus scrape job fields.
- [x] Preserve static `juju_*` labels from related scrape jobs.
- [x] Add local Alloy and unix-exporter metrics forwarding into the same remote write path.
- [x] Render one `prometheus.remote_write` component for the related Mimir endpoint.
- [x] Perform an early live validation against the currently deployed `mimir-vm` cluster and confirm a local unix-exporter metric from Alloy is queryable in Mimir.

## Phase C - Safe no-upstream behavior

- [x] Disable relation-derived scrape jobs when no remote write endpoint exists.
- [x] Keep local self metrics dropped rather than buffered when no upstream exists.
- [x] Configure bounded remote write outage buffering with `max_keepalive_time = "30m"`.
- [x] Add status messaging for scrape-targets-present but remote-write-missing.
- [x] Document the no-upstream and outage-buffer behavior in the README.

## Phase D - Topology and integration validation

- [x] Validate source topology labels from local-model scrape targets.
- [x] Validate source topology labels from cross-model scrape targets where networking permits.
- [x] Validate end-to-end Alloy -> `dummychain` -> `mimir-vm`.
- [x] Validate multiple target units scraped by one Alloy unit.
- [x] Record any required follow-up work for `loki-vm` as a scrape provider.

Phase D validation notes:

- Local-model validation succeeded against `dummychain/5` and `dummychain/7`; Mimir received `up` with preserved `juju_model`, `juju_application`, `juju_unit`, and `juju_charm` labels for both units.
- Cross-model validation succeeded with `cmr-dummychain/0` from model `alloy-scrape-cmr`; Mimir received `alloy_build_info` with `juju_model="alloy-scrape-cmr"` and `juju_unit="cmr-dummychain/0"`.
- One Alloy unit successfully scraped multiple related units from a single `prometheus_scrape` relation and forwarded them to `mimir-vm`.
- `loki-vm` follow-up remains provider-side only: add a provided `metrics-endpoint` relation using `MetricsEndpointProvider`, publish a routable metrics target, and prefer wildcard unit targets so `juju_unit` is preserved.

## Phase E - Provider follow-ups

- [ ] Add a concrete provider-side checklist for `dummychain`.
- [ ] Add a concrete provider-side checklist for `loki-vm`.
- [ ] Document provider requirements: wildcard targets, routable metrics endpoint, standard library usage.
