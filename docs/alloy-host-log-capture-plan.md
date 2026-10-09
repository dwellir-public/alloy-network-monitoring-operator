# Alloy Host Log Capture Plan

## Goal

Extend `alloy-vm` in the smallest useful way so it can collect more than named
systemd unit logs from the host.

Release 1 scope:

- kernel-level messages such as `dmesg`/journald kernel transport
- a small advanced escape hatch for extra journald matches

This is a planning document only.

## Current state

Today `alloy-vm` renders:

- one `loki.source.journal "journald"` block
- `matches = "_SYSTEMD_UNIT=<unit> OR ..."`
- a fixed path from matched journal entries into the Juju-labeled Loki pipeline

So current host log capture is:

- good for explicitly named host services
- not suitable for kernel transport logs
- not suitable for broader host log classes

## What we want to support

Release 1 should only cover:

1. named services
2. kernel messages
3. explicit raw journald matches for advanced operators

Examples:

- `ssh.service`
- `snap.lxd.daemon.service`
- kernel transport logs
- `SYSLOG_IDENTIFIER=lxd`

## Key design constraint

`loki.source.journal` matches journald fields, not classic rsyslog facility
syntax.

So release 1 should not attempt a “facility” abstraction.

## Recommended design

Use a minimal “journal selectors” model.

Suggested config surface:

- keep `systemd-units` as-is for compatibility
- add `journal-kernel`
- add `journal-match-expressions`

### 1. `journal-kernel`

Type:

- boolean

Behavior:

- when enabled, include journal entries matching kernel transport

Likely match shape:

- `_TRANSPORT=kernel`

Purpose:

- capture the kernel log stream that operators usually think of as `dmesg`

### 2. `journal-match-expressions`

Type:

- newline-separated raw journald match expressions

Examples:

- `_TRANSPORT=kernel`
- `PRIORITY=3`
- `SYSLOG_IDENTIFIER=lxd`
- `_SYSTEMD_UNIT=snap.lxd.daemon.service`

Behavior:

- append these expressions directly into the generated Alloy journal match expression

Purpose:

- provide an escape hatch for advanced operators without requiring a full config override

## Why this is better than only adding `dmesg`

Only adding kernel capture solves one case.

Adding raw journald match expressions as well gives a precise operator escape
hatch without turning this into a broad log taxonomy feature.

## Proposed release phases

- [ ] Phase 0: Lock the config model
- [ ] Phase 1: Add `journal-kernel` config and render `_TRANSPORT=kernel`
- [ ] Phase 2: Add `journal-match-expressions` as an advanced escape hatch
- [ ] Phase 3: Add unit tests for rendered match combinations
- [ ] Phase 4: Update README with operator examples for LXD hosts and kernel failure tracking
- [ ] Phase 5: Validate on a real machine with:
  - host kernel messages
  - `snap.lxd.daemon.service`
  - `ssh.service`
  - `alloy-vm/1` as the VM-shaped validation target
  - `alloy-vm/0` as the LXC container regression check because its log behavior differs

## Recommended order

Implement in this order:

1. `journal-kernel`
2. `journal-match-expressions`

Reason:

- kernel capture is the most concrete missing operator need
- raw match expressions give flexibility immediately

## Recommended operator examples

### Capture host services and kernel logs

```bash
juju config alloy-vm systemd-units='ssh.service,snap.lxd.daemon.service'
juju config alloy-vm journal-kernel=true
```

### Capture kernel logs and a custom journald selector

```bash
juju config alloy-vm journal-kernel=true
juju config alloy-vm journal-match-expressions=$'SYSLOG_IDENTIFIER=lxd'
```

## Decisions

1. `journal-match-expressions` should be raw and powerful.
2. Broader host journal capture should not add Juju topology labels.
3. Release 1 should not add priority-based filtering support.

## Recommendation

For the first implementation:

- add `journal-kernel=true`
- add `journal-match-expressions`
- do not add `journal-facilities`
- do not add Juju topology labels to the broader host journal path
- do not add priority-based filtering

That gives immediate value for machine-level failure capture without pretending the syslog facility model maps perfectly onto journald everywhere.
