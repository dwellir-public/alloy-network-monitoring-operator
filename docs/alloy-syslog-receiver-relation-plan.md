# alloy-vm Syslog Receiver Relation Plan

This document plans the Alloy-side feature work needed so machine charms such
as `haproxy-dataplane-api` can send logs to Alloy over Juju relations instead
of assuming a static host and port.

The target flow is:

1. a requirer charm relates to Alloy over `syslog-receiver`,
2. Alloy publishes receiver connection details over relation data,
3. the requirer configures its workload to send syslog to Alloy, and
4. Alloy forwards those logs onward to `loki-vm` through `send-loki-logs`.

## Current state

Already implemented in Alloy:

- `syslog-receiver` is declared as a provided relation in
  [charmcraft.yaml](/home/erik/Loki-project/alloy-vm-operator/charmcraft.yaml)
- Alloy can listen for remote syslog on TCP and UDP `:1514` when
  `enable-syslogreceivers=true`
- Alloy forwards received syslog to Loki when `send-loki-logs` is related
- Alloy drops received syslog if no Loki endpoint exists, avoiding local disk
  buffering

Not yet implemented:

- no provider-side relation logic for `syslog-receiver`
- no publication of receiver address, port, protocol, or readiness state
- no documented contract for requirer charms like `haproxy-dataplane-api`

## Feasibility

This is straightforward.

Alloy already has the runtime behavior needed to receive and forward syslog.
The missing piece is the relation contract and provider logic.

## Decisions to make

### 1. Relation payload shape

Decision:

- publish one active receiver address plus one port, and list supported
  protocols explicitly

Reason:

- release 1 uses a single Alloy unit
- the current implementation listens on the same port for both TCP and UDP
- the requirer only needs enough data to configure its local syslog sender

Release-1 payload:

- `address`
- `port`
- `protocols` such as `tcp,udp`
- `ready`
- `recommended-protocol`
- `reason`

### 2. Readiness semantics

Decision:

- only publish a ready receiver when all of these are true:
  - `enable-syslogreceivers=true`
  - at least one `send-loki-logs` endpoint exists

Reason:

- without an upstream Loki path, Alloy deliberately drops syslog
- publishing a ready receiver while dropping logs would be misleading

### 3. Protocol support in release 1

Decision:

- advertise both TCP and UDP
- let requirers choose TCP by default

Reason:

- Alloy already renders both listeners
- TCP is usually the safer default for structured forwarding from another
  service

### 4. Topology labeling of forwarded syslog

Decision:

- keep the current transparent proxy behavior in release 1
- do not add Juju topology automatically to relation-originated syslog

Current behavior:

- no Juju topology labeling is added to incoming syslog
- only receiver/syslog-derived labels are added

Follow-up option for later:

- if better source attribution is needed later, add explicit relation labels
  such as source app or relation name rather than overloading existing syslog
  metadata

### 5. Address publication

Decision:

- publish the binding ingress address for `syslog-receiver`
- fall back to the unit ingress address if needed

Reason:

- the charm already has helper logic for receiver IP selection
- requirers need a routable unit address, not `0.0.0.0`

## Phases

## Phase A - Relation contract definition

- [x] Document the `syslog-receiver` provider contract in Alloy docs
- [x] Decide the exact relation keys to publish
- [x] Decide readiness behavior
- [x] Decide whether TCP is the default recommended protocol for requirers

## Phase B - Provider implementation

- [x] Add provider-side relation handling for `syslog-receiver`
- [x] Publish receiver address, port, protocols, and readiness state
- [x] Reconcile relation data on:
  - [x] config changes
  - [x] Loki endpoint changes
  - [x] Alloy service state changes
  - [x] relation lifecycle events
- [x] Ensure relation data is cleared or marked not ready when syslog reception
      is disabled

## Phase C - Unit tests

- [x] Add unit tests for published relation data when syslog receiver is enabled
- [x] Add unit tests for not-ready behavior when Loki is absent
- [x] Add unit tests for disabled receiver behavior
- [x] Add unit tests for address and protocol publication

## Phase D - Integration validation

- [ ] Validate Alloy publishes usable syslog receiver data in a live model
- [ ] Validate a related charm can configure itself from the published data
- [ ] Validate end-to-end flow:
  - [ ] source charm -> Alloy syslog receiver
  - [ ] Alloy -> `loki-vm`

## Explicit dependency for HAProxy integration

`haproxy-dataplane-api` should depend on this Alloy feature being present.

Without it:

- Alloy can receive syslog operationally,
- but there is no clean relation-driven way for HAProxy to discover how to send
  logs to Alloy.
