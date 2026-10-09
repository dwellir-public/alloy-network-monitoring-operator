# alloy-vm Syslog Drop Controls Plan

This document plans a follow-up feature for Alloy so remote syslog received
over `syslog-receiver` can be reduced before it is forwarded to `loki-vm`.

The immediate driver is `haproxy-dataplane-api`, where HAProxy access logs can
produce a much higher event rate than service or application logs.

## Goal

Allow Alloy to act as a second guardrail for log volume when a related charm is
already sending syslog to Alloy, but the operator does not want every message
to be retained in Loki.

This is not a replacement for source-side suppression. The preferred order is:

1. suppress noisy logs at the source charm when possible,
2. use Alloy drop/limit controls as a second line of defense, and
3. keep Loki retention finite.

## Current state

Already implemented in Alloy:

- `loki.source.syslog "receiver"` listens on TCP and UDP `:1514`
- received syslog is forwarded directly to `loki.write.main`
- no Juju topology is added to incoming syslog
- receiver labels such as `source_ip`, `protocol`, and `syslog_hostname` are
  preserved when parsing succeeds

Current limitation:

- there is no intermediate `loki.process` stage dedicated to remote syslog
- there are no config knobs to drop, sample, or rate-limit noisy syslog input

## Feasibility

This is feasible in Alloy using `loki.process`.

Relevant upstream support:

- `stage.drop` can remove lines matching expressions or field values
- `stage.limit` can rate-limit entries that survive previous stages

Release 1 of this feature should keep the scope narrow and only cover remote
syslog received through `syslog-receiver`.

## Decisions to make

### 1. Scope of filtering

Decision:

- apply filtering only to remote syslog received through
  `loki.source.syslog "receiver"`

Reason:

- local journal logs and other future log sources should not be affected by
  syslog-specific drop policies

### 2. Configuration model

Decision:

- start with explicit charm config, not relation-driven policy

Suggested config direction:

- `syslog-drop-access-logs` as a boolean
- `syslog-drop-expressions` as newline-separated patterns
- `syslog-rate-limit` and `syslog-rate-burst` as integer controls for
  surviving syslog entries

Reason:

- operators need a clear, local control surface first
- relation-driven policy negotiation is unnecessary complexity in release 1

### 3. Default behavior

Decision:

- default to no Alloy-side dropping

Reason:

- source-side behavior should remain the primary policy decision
- Alloy-side dropping should be an explicit operator choice

### 4. Matching strategy

Decision:

- start with line-expression matching only in release 1
- defer label-aware filtering to a later follow-up if needed

Practical target for HAProxy:

- drop access-log lines matching request-style patterns such as:
  - `"GET "`
  - `"POST "`
  - `" haproxy/<NOSRV> "`

### 5. Rate limiting semantics

Decision:

- apply rate limiting only after explicit drops
- render `stage.limit` only when `syslog-rate-limit > 0`
- default `burst` to the configured rate when `syslog-rate-burst` is unset

Reason:

- drop rules should remove known-noisy traffic first
- limit rules should protect Loki from bursts that survive those drops

## Phases

## Phase A - Config and pipeline design

- [x] Decide the exact charm config surface for remote syslog filtering
- [x] Add a dedicated `loki.process` stage for remote syslog instead of
      forwarding directly from `loki.source.syslog`
- [x] Document the intended precedence:
      source suppression first, Alloy drop/limit second

## Phase B - Config builder implementation

- [x] Render a remote-syslog-specific `loki.process` pipeline
- [x] Implement `stage.drop` support for configured expressions
- [x] Implement `stage.limit` support for optional rate limiting
- [x] Keep local journal log forwarding unaffected

## Phase C - Unit tests

- [x] Add config-builder tests for no-op behavior when filters are disabled
- [x] Add config-builder tests for drop-stage rendering
- [x] Add config-builder tests for limit-stage rendering
- [x] Add charm tests for config change and validation behavior

## Phase D - Live validation

- [ ] Validate that a related source can still send syslog successfully
- [ ] Validate that configured access-log patterns are dropped before Loki
- [ ] Validate that non-matching remote syslog still reaches Loki
- [ ] Validate that local journal logs are unaffected

## Expected operator outcome

With this feature in place, an operator should be able to choose one of these
operating modes:

- keep all remote syslog
- drop common HAProxy-style access logs while keeping service/app messages
- rate-limit surviving remote syslog during bursts

This keeps Alloy useful as a shared remote-syslog receiver without forcing Loki
to retain every high-volume request log.
