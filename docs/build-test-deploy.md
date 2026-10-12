# Build, test and deploy

Run commands from the checkout root. Local tests verify source behavior; they do
not establish real-device compatibility or live ingestion.

## Local checks

```bash
uv sync --locked --group dev
uv run tox -e lint,static,unit,provenance
```

The binary suite runs generated discovery rules in Alloy with loopback HTTP
fixtures. Supply the tested Alloy 1.20.1 executable:

```bash
python3 tools/fetch_alloy.py --output build/alloy-linux-amd64
uv run pytest tests/binary --alloy-binary=build/alloy-linux-amd64
```

It checks eligibility, both profile selections, final labels, protected component
output, discovery failure retention, successful empty inventory and invalid
reload recovery. It needs local sockets. It does not simulate successful SNMP
collection from an appliance. The download verifies the tested executable's
SHA-256; PR CI runs this same native suite without first formatting source.

## Build artifacts

```bash
charmcraft pack --platform ubuntu@22.04:amd64
charmcraft pack --platform ubuntu@24.04:amd64
```

Expected artifacts are `alloy-network-monitoring_ubuntu@22.04-amd64.charm` and
`alloy-network-monitoring_ubuntu@24.04-amd64.charm`. Verify each built inventory:

```bash
python3 tools/dependency_bom.py \
  --artifact alloy-network-monitoring_ubuntu@24.04-amd64.charm \
  --base ubuntu@24.04 --arch amd64 --output build/runtime-24.cdx.json
```

Use the corresponding artifact and base for 22.04. Fresh installations pin Alloy
1.20.1-1. Builds include the unchanged, checksum-pinned `cos-tool` binary.

## Explicit live artifact test

The pytest/Jubilant test requires a selected nonproduction model and built charm
path. It never uses the current Juju model implicitly. It creates a unique test
application, validates running Alloy configuration, checks the destination gate,
and removes that application at the end. It does not remove the model.

```bash
TEST_MODEL=controller:alloy-lab \
TEST_MODEL_IS_NONPRODUCTION=yes \
CHARM_PATH=./alloy-network-monitoring_ubuntu@24.04-amd64.charm \
TEST_BASE=ubuntu@24.04 \
uv run pytest tests/integration -q
```

Repeat for 22.04 with its matching artifact/base. The suite skips when no explicit
model or artifact is provided. This command deploys a workload and incurs the
selected model's machine cost; use a disposable lab model.

For actual relation/backend and refresh checks, provide backend integrations
already available in the selected nonproduction model and their query URLs:

```bash
TEST_MODEL=controller:alloy-lab \
TEST_MODEL_IS_NONPRODUCTION=yes \
CHARM_PATH=./alloy-network-monitoring_ubuntu@24.04-amd64.charm \
PREVIOUS_CHARM_PATH=/path/to/previous-24.04.charm \
TEST_BASE=ubuntu@24.04 \
TEST_REMOTE_WRITE_PROVIDER=mimir:receive-remote-write \
TEST_MIMIR_QUERY_URL=http://mimir.example/prometheus/api/v1/query \
TEST_LOKI_PROVIDER=loki:loki_push_api \
TEST_LOKI_QUERY_URL=http://loki.example/loki/api/v1/query_range \
uv run pytest tests/integration -q
```

Loki provider/query inputs must be supplied together; omitted Loki inputs leave
log ingestion unverified. The backend test skips if its required Mimir inputs
are absent. Supplying a previous artifact establishes an old-to-new refresh;
without it, refresh uses the current artifact and cannot prove that upgrade path.

The test creates uniquely named applications, fake Juju secrets and loopback
inventory/metrics sources. A colocated fixture publishes real provider topology
using the unchanged v3 library. It verifies fresh Mimir/Loki labels, logging
validation, credential rotation/withdrawal and PID/start-time/WAL continuity.
It fixes listener arguments before the refresh so the continuity check covers
unchanged runtime arguments. The SNMP target has no agent: `up=0` confirms
discovery, labeling and delivery, while real interface counters need appliance
acceptance. Cleanup removes only owned applications, secrets and newly allocated
empty machines, retaining the selected model and existing workloads. Redacted
observations go to `build/live-evidence/` or `TEST_EVIDENCE_DIR`.

## NetBox, device and backend acceptance

Record the installed NetBox and discovery-plugin versions before the live run.
Confirm the hosted NetBox permits the plugin and that its device endpoint emits
the metadata described in the [operator guide](network-monitoring.md). Confirm
region and repeated-tag filters against that installed version.

Configure the built application with granted secrets and the existing remote
write integration. Enroll an OPNsense device with a dedicated primary IPv4 and a
known `snmp_auth` profile, then add a Cisco device. Verify `system,if_mib` metrics,
original interface labels, `job="snmp/network"`, management IP instances and
actual site labels in Mimir. Change enrollment and confirm target withdrawal
within a refresh cycle without a PID change.

Exercise secret revision updates, profile removal, grant revocation, discovery
outages, successful empty responses and invalid configuration. Confirm unrelated
metrics/log forwarding and existing source identity throughout. Verify network
rules arrive through the existing rule transport and evaluate in the ruler.

Inspect the service's actual `STORAGE_PATH` and process storage arguments. Create
a remote-write outage shorter than the retention bound, confirm buffering,
restart Alloy, recover the endpoint and verify queued delivery. Refresh the built
charm while watching the PID, WAL directory and device labels. Record actual
results; a source-level refresh test alone does not prove live continuity.

## Current verification scope

Validation on 2026-10-12 passed lint/format/type and dependency-provenance checks,
324 unit tests and six native Alloy tests. Rebuilt Ubuntu 22.04/24.04 amd64
artifacts were inspected for runtime sources, relation preservation and private
input exclusions, then exercised against Mimir 3.2.1 and Loki 3.7.8 in a lab.
Both bases passed collector/provider identity, secret rotation/withdrawal,
logging validation and unchanged-argument refresh PID/start-time/WAL checks.
Syslog ingestion retained 14 indexed labels and its auxiliary metadata.

The synthetic SNMP target had no agent; its diagnostic `up=0` proves routing and
delivery, not appliance authentication or interface counters. Real OPNsense/Cisco
acceptance, installed NetBox/plugin behavior, Grafana browser checks and alert
firing/runbooks still need verification. Interface enrichment and HA remain later
milestones. Per-run logs, audit snapshots and implementation notes are local
working records rather than maintained repository documentation.
