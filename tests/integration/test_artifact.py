"""Validate the built artifact in an explicitly selected nonproduction Juju model."""

import os
import uuid
from pathlib import Path

import jubilant
import pytest


@pytest.fixture
def deployment():
    model = os.environ.get("TEST_MODEL", "")
    artifact = os.environ.get("CHARM_PATH", "")
    if not model or not artifact:
        pytest.skip("Set TEST_MODEL and CHARM_PATH for live artifact tests")
    if os.environ.get("TEST_MODEL_IS_NONPRODUCTION") != "yes":
        pytest.fail("Set TEST_MODEL_IS_NONPRODUCTION=yes after verifying the selected model")
    path = Path(artifact).resolve()
    assert path.is_file() and path.suffix == ".charm"
    base = os.environ.get("TEST_BASE", "ubuntu@24.04")
    assert base in {"ubuntu@22.04", "ubuntu@24.04"}
    juju = jubilant.Juju(model=model, wait_timeout=1200)
    app = "alloy-network-test-" + uuid.uuid4().hex[:8]
    assert app not in juju.status().apps
    deployed = False
    try:
        juju.deploy(path, app=app, base=base)
        deployed = True
        yield juju, app
    finally:
        if deployed:
            juju.remove_application(app)
            juju.wait(lambda status: app not in status.apps)


def test_artifact_runtime_and_network_destination_gate(deployment):
    juju, app = deployment
    juju.wait(lambda status: jubilant.all_active(status, app))
    unit = next(iter(juju.status().apps[app].units))
    juju.cli(
        "ssh", unit, "--", "sudo", "-u", "alloy", "alloy", "validate", "/etc/alloy/config.alloy"
    )
    version = juju.cli("ssh", unit, "--", "alloy", "--version")
    assert "v1.20.1" in version
    juju.config(
        app,
        {
            "network-monitoring-enabled": True,
            "collector-id": app,
            "netbox-url": "https://netbox.example.invalid",
        },
    )
    juju.wait(
        lambda status: all(
            unit.workload_status.current == "waiting" for unit in status.apps[app].units.values()
        )
    )
    config = juju.cli("ssh", unit, "--", "sudo", "cat", "/etc/alloy/config.alloy")
    assert 'discovery.http "network_netbox"' not in config
    juju.config(app, {"network-monitoring-enabled": False})
    juju.wait(lambda status: jubilant.all_active(status, app))
