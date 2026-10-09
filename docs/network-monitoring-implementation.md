# Network monitoring implementation record

This implements the [audited plan](network-monitoring-audit.md) from source
`237aab5e99f6b92f8505c0647c9213424105a330`. The user's latest instructions
supersede the audit's parameter names: use `netbox-token` and `snmp-auth`, both
holding Juju secret references. One SNMP secret supports multiple named SNMPv3
authentication profiles, selected by each device's `snmp_auth` custom field.
Site, region and tags are independently optional. Preserve all existing relation
definitions, vendored libraries, data schemas and remote-write destinations.

## Before, target and actual results

Authority and applicability are recorded separately. Actual results below are
local evidence; live infrastructure acceptance remains conditional.

| Requirement | Authority / applicability | Before | Disposition | Projected target / check | Actual |
| --- | --- | --- | --- | --- | --- |
| Charm name | task-required / charm packaging | `alloy-vm` | Implement | Rename metadata/package/artifacts; existing packaging tests | `alloy-network-monitoring` in metadata, project and both packed artifacts; regression checks pass |
| Optional inventory filters | task-required / network collection | Absent | Implement | Omit empty site/region/tags; encoded repeated tag parameters; fixture tests | Site, region and tags independently optional; repeated tags use AND; rendering and binary filter checks pass; installed region semantics need live verification |
| Juju secrets and multiple authentications | task-required / network credentials | Absent | Implement | Config references only; named v3 profiles; secret rotation/access/removal tests | `netbox-token` and `snmp-auth`; up to 128 named v3 profiles; rotation, removal, grant loss and file permission tests pass; actual Alloy redacts secret inputs |
| Native Alloy discovery and device labels | task-required / network collection | Absent | Implement | Actual Alloy fixture tests; stable device instance and preserved interface data | Actual Alloy HTTP-SD fixture selects approved IPv4 targets/profiles and emits stable device labels; discovery updates require no restart; real interface samples need live SNMP acceptance |
| Existing relation interfaces | task-required / all existing integrations | Working user infrastructure; source definitions available | Preserve | Byte-identical vendored libraries and unchanged relation definitions; regression suite | Relations match upstream exactly; vendored source and packed checksums match the original inventory; regression suite passes |
| Validated configuration application | task-required / config lifecycle | `fmt`; applied state advances before reload | Implement | Semantic validation, protected secret files, failed apply recovery and retry tests | Semantic validation, immutable credential generations, synchronous managed reload/readiness, rollback and retry checks pass; applied state advances after success |
| Packaging | required-baseline / shared charm | Static BUILD-001 criteria satisfied | Preserve | Locked dependencies, uv plugin, consolidated definitions and artifact checks | Charmcraft builds Ubuntu 22.04 and 24.04 amd64 artifacts; each matches source, 13 locked runtime packages and vendored inventory; lock check passes |
| Workload/source identity | required-baseline / existing observability; task-required / network metrics | Source-specific existing renderer; no fresh live evidence | Preserve and extend | Existing tests; network metrics omit collector Juju ownership; backend evidence remains conditional | Existing topology regression checks pass; device targets use approved inventory identity; self/host metrics retain Juju topology; real backend samples remain unverified |
| Charm refresh safety | required-baseline / existing workload | Hook skips rewrite/restart; live continuity unverified | Preserve | Event assertions and documented refresh behavior; live refresh conditional on lab | Upgrade event, including library-generated events and pending credential revisions, does not install/rewrite/restart; live Juju refresh continuity remains unverified |
| Runtime behavior tests | required-baseline / built charm; task-required / actual Alloy | Existing deploy-only integration test | Implement local binary tests and portable lab tests | pytest/Jubilant artifact test; discovery, secrets and ingestion behavior | 317 unit tests and four actual Alloy tests pass; one explicit-model Jubilant artifact test skips because no nonproduction model was supplied |
| WAL persistence | task-required / runtime state | Stable writer name, bounded WAL; actual service storage unverified | Preserve and document | Keep writer/storage identity; binary restart tests; live storage/refresh checks conditional | Stable writer and service storage settings preserved; binary test observes SNMP labels in WAL, segment replay and delivery after outage/restart; live service storage remains unverified |
| Network alerts | task-required / operational checks | Absent | Implement within existing rule transport | Owned network groups, preserved schema/cache/encoding; no new relation fields | Six collector-scoped alerts use existing groups/transport; rule schema and PromQL validate with packaged cos-tool; downstream evaluation needs live acceptance |
| Documentation | recommended-default / repository docs | Old names and workstation paths | Implement | Operator secret examples, architecture, developing and tested-version record | Operator guide, two-profile example, architecture and portable build/lab instructions updated; local links and spelling checked |
| External sink authentication/tenant changes | task-required / user's preservation instruction | Existing integration works | Defer | No new Mimir config or relation contract changes in this implementation | Deferred |
| Interface enrichment, HA, discovery snapshot cache | task-required / original staged scope | Absent | Defer | Follow-up milestones; one active collector supported initially | Deferred |

Local implementation and validation are authorized. Deployment, publication,
Git remote changes and credential provisioning are not being performed. NetBox
plugin availability and a nonproduction Juju model/device/backend setup remain
inputs for live acceptance. Existing audit/evidence files and `PLAN.md` remain
historical task inputs.

## Verification evidence

The [verification manifest](network-monitoring-implementation-evidence/verification.json)
records exact source and artifact SHA-256 hashes, upstream identity, workload
binary identity and local checks. Final validation used Python 3.12.15, Alloy
1.20.1 and Charmcraft 4.4.2:

- Unit/integration invocation: 317 passed, one live artifact test skipped,
  52 warnings.
- Actual Alloy fixture invocation: four passed. Fixtures cover discovery
  updates, failed versus empty refresh, profile selection, secret redaction,
  valid/invalid reloads and WAL delivery after an outage and forced restart.
- Ruff lint and formatting, Pyright and Codespell passed. Tool lint and
  formatting also passed. The locked dependency graph checked successfully.
- Both Ubuntu artifacts contain matching runtime source, the expected 13
  runtime distributions, unchanged vendored libraries and the pinned cos-tool.
  The network alert rules validate with that tool.

The WAL fixture includes the collector's periodic self scrape. It demonstrates
local buffering and replay with the standard writer and a local remote-write
receiver; it does not establish real Mimir authentication, tenant isolation or
an unlimited outage retention guarantee. Neither real devices nor an installed
NetBox plugin nor a live Juju refresh was exercised. Follow the
[lab acceptance instructions](build-test-deploy.md) before rollout.

## Separate repository and resuming work

The user chose a separate repository and removed the original Git metadata.
A fresh local Git repository was initialized on `main` for this charm. Its first
commit includes the inherited source, network monitoring implementation, tests
and documentation. The upstream commit above and the historical audit retain the code's
origin. Renaming the directory does not alter repository identity; reopen the
renamed directory as the workspace to update tool paths and write permissions.

Development and Ubuntu 24.04 CycloneDX generation passed before the original
Git metadata was removed. Both final artifact inventories have also been
verified directly. Generating complete CycloneDX records for the new repository
requires its first commit because the provenance tool records `HEAD` and the
working diff. No commit, remote creation or publication is needed to resume
local development.
