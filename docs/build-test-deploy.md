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
uv run pytest tests/binary --alloy-binary=/path/to/alloy
```

It checks eligibility, both profile selections, final labels, protected component
output, discovery failure retention, successful empty inventory and invalid
reload recovery. It needs local sockets. It does not simulate successful SNMP
collection from an appliance.

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

## NetBox, device and backend acceptance

Record the installed NetBox and discovery-plugin versions before the live run.
Confirm the hosted NetBox permits the plugin and that its device endpoint emits
the metadata described in the [operator guide](network-monitoring.md). Confirm
region and repeated-tag filters against that installed version.

Configure the built application with granted secrets and the existing remote
write integration. Enroll an OPNsense device with a dedicated primary IPv4 and a
known `snmp_auth` profile, then add a Cisco device. Verify `system,if_mib` metrics,
original interface labels, `job="snmp/network"`, stable device instances and
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

See the [implementation record](network-monitoring-implementation.md) for what
has actually run in this workspace. Interface enrichment and HA remain later
milestones.
