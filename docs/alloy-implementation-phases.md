# alloy-vm Implementation Phases

This document consolidates the Alloy VM charm scope, implementation conventions, and phased delivery plan.

## Scope

The `alloy-vm` charm is a machine charm for Grafana Alloy on Ubuntu. It should install `grafana-alloy`, manage the systemd service, and support Ubuntu 22.04 and 24.04.

Release 1 is focused on logging:

- install Alloy from the official Grafana APT repository using the same pattern as `loki-vm`
- support a full configuration override, following the `loki-vm` operator pattern
- preserve the package default config so the charm can revert to a known-good baseline
- validate generated or operator-supplied config before applying it
- retain invalid configs under `/tmp/alloy-config-invalid-*.yaml` for debugging
- surface invalid config or on-disk drift through charm status and logs
- include a generated-config header warning about overwrites
- enforce Alloy user ownership on charm-managed files and storage paths
- send local systemd journal logs to Loki over `send-loki-logs`
- optionally receive remote syslog on TCP and UDP `1514` and forward it to Loki without adding Juju topology

Later-stage features can extend Alloy beyond logging:

- `prometheus_scrape`
- `prometheus_remote_write`
- tracing receive/send
- profiling receive/send

## Implementation Conventions

- Use the `ops` framework for charm logic.
- Keep Juju logic thin and encapsulate workload behavior in a standalone `src/alloy.py` module with no charm dependencies.
- Use a dedicated `ConfigBuilder` in `src/config_builder.py`.
- Manage dependencies through the `uv` charmcraft plugin.
- Cover workload and charm behavior with unit tests, and add integration tests for deployed behavior.

## Current Direction

The charm has already been refactored away from a single-file, ad-hoc implementation toward the same structure used by `loki-vm`:

- workload operations are handled in `src/alloy.py`
- config composition is handled in `src/config_builder.py`
- the charm stores last-known-good state and handles config drift
- logging to Loki and syslog receiver support are implemented

The remaining work is primarily actions, broader tests, docs, and later-stage telemetry features.

## Relations

Implemented or planned relations for the Alloy VM charm:

1. `send-loki-logs` using interface `loki_push_api`
2. `syslog-receiver` using interface `syslog`

Post-release logging extensions:

1. `prometheus_scrape`
2. `prometheus_remote_write`
3. tracing relations
4. profiling relations

## Phases

## Phase 0 - Baseline alignment

- [x] Update `charmcraft.yaml` metadata to match the Alloy VM charm scope.
- [x] Confirm Ubuntu 22.04 amd64 support.
- [x] Confirm Ubuntu 24.04 amd64 support.
- [x] Switch charmcraft build to the `uv` plugin.
- [x] Align config options with the planned config override pattern.
- [x] Add placeholder relations in `charmcraft.yaml`:
  - `send-loki-logs` using interface `loki_push_api`
  - `syslog-receiver` using interface `syslog`

## Phase 1 - Workload install and service control

- [x] Create `src/alloy.py` as a workload helper independent of charm code.
- [x] Implement `install()` using the Grafana APT repository pattern used by `loki-vm`.
- [x] Implement service lifecycle helpers:
  - `start()`
  - `stop()`
  - `restart()`
  - `reload()`
- [x] Implement `get_version()`.
- [x] Replace direct `subprocess` usage in the charm with workload helper methods.

## Phase 2 - Config builder and persistence

- [x] Create `src/config_builder.py`.
- [x] Manage the main config at `/etc/alloy/config.alloy`.
- [x] Preserve the package default config as `/etc/alloy/config.alloy.package-default`.
- [x] Add a generated-config header in rendered configs.
- [x] Validate candidate config with `alloy fmt <file>` before applying.
- [x] Persist invalid configs under `/tmp/alloy-config-invalid-*.yaml`.
- [x] Ensure Alloy user ownership for managed files and storage paths.

## Phase 3 - Charm orchestration basics

- [x] Introduce `StoredState` for:
  - `last_good_config`
  - `last_failed_config_path`
  - `config_drifted`
- [x] Wire key events:
  - `install`
  - `start`
  - `config_changed`
  - `upgrade_charm`
- [x] Ensure `upgrade_charm` does not restart Alloy or rewrite config.
- [x] Publish workload version from `get_version()`.

## Phase 4 - Config overrides and drift detection

- [x] Add `config-override` as the only operator-supplied full-config option.
- [x] Regenerate config via `ConfigBuilder` when `config-override` is empty.
- [x] Detect manual on-disk edits and surface Maintenance status with warning logs.
- [x] Clear drift-related Maintenance when on-disk and charm-expected config match again.
- [x] Remove the legacy config option in favor of `config-override`.

## Phase 5 - Logging relation to Loki

- [x] Add `LokiPushApiConsumer` integration for `send-loki-logs`.
- [x] Support forwarding selected local systemd unit logs to Loki.
- [x] Add a config option listing systemd units to monitor.
- [x] Add Juju topology labels to locally collected logs.
- [x] Drop local logs when no Loki relation exists to avoid unnecessary disk usage.

## Phase 6 - Syslog receiver integration

- [x] Add a live-debugging config option for Alloy.
- [x] Add `syslog-receiver` support to accept remote syslog messages.
- [x] Add a config option to enable syslog receivers, defaulting to disabled.
- [x] Listen on both TCP and UDP port `1514` when enabled.
- [x] Avoid adding Juju topology to incoming syslog logs.
- [x] Add basic receiver labels such as hostname and receiver IP.
- [x] Forward received syslog to Loki as a transparent proxy.
- [x] Drop received syslog when no Loki relation exists to avoid buffering on disk.
- [x] Publish `syslog-receiver` relation data with receiver address, port,
      protocols, readiness, and recommended protocol.
- [x] Add workload health checks in `update-status`.

## Phase 7 - Actions and upgrades

- [ ] Add action `alloy-service` for `start`, `stop`, `restart`, and `reload`.
- [ ] Add action `show-runtime-config`.
- [ ] Add action `upgrade-alloy` with optional version pinning and safe upgrade flow.

## Phase 8 - Tests and docs

- [ ] Add focused unit tests for `src/alloy.py`.
- [ ] Add focused unit tests for config rendering and validation behavior.
- [ ] Add deployment and relation integration tests with `pytest` and `jubilant`.
- [ ] Update `README.md` with usage, config, actions, and relation behavior.

## Phase 9 - Later-stage telemetry features

- [ ] Implement `prometheus_scrape` support.
- [ ] Implement `prometheus_remote_write` support.
- [ ] Implement tracing receive/send support.
- [ ] Implement profiling receive/send support.

Related planning:

- See `docs/alloy-metrics-scrape-feature-plan.md` for the dedicated metrics scrape and remote write feature plan.
- See `docs/alloy-syslog-receiver-relation-plan.md` for the provider-side syslog receiver relation plan used by charms such as `haproxy-dataplane-api`.
- See `docs/alloy-syslog-drop-controls-plan.md` for the follow-up plan to drop or rate-limit remote syslog before forwarding it to `loki-vm`.
