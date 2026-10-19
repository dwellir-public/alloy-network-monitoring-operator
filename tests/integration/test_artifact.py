"""Validate the built artifact in an explicitly selected nonproduction Juju model."""

import hashlib
import json
import os
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import jubilant
import pytest
from live_helpers import (
    build_source_fixture,
    emit_logs,
    indexed_log_labels,
    network_target_modules,
    query,
    rotate_source,
    set_inventory_modules,
    snapshot,
    start_source,
    wait_for,
)


@dataclass
class Deployment:
    """A uniquely named application owned and cleaned up by this test."""

    juju: jubilant.Juju
    app: str
    artifact: Path
    base: str


@pytest.fixture
def artifact(request):
    path = os.environ.get("CHARM_PATH", "")
    if not path:
        pytest.skip("Set CHARM_PATH for live artifact tests")
    if getattr(request, "param", "current") == "prior":
        path = os.environ.get("PREVIOUS_CHARM_PATH", "") or path
    result = Path(path).resolve()
    assert result.is_file() and result.suffix == ".charm"
    return result


@pytest.fixture
def deployment(artifact):
    model = os.environ.get("TEST_MODEL", "")
    if not model:
        pytest.skip("Set TEST_MODEL and CHARM_PATH for live artifact tests")
    assert ":" in model, "TEST_MODEL must be controller-qualified"
    if os.environ.get("TEST_MODEL_IS_NONPRODUCTION") != "yes":
        pytest.fail("Set TEST_MODEL_IS_NONPRODUCTION=yes after verifying the selected model")
    base = os.environ.get("TEST_BASE", "ubuntu@24.04")
    assert base in {"ubuntu@22.04", "ubuntu@24.04"}
    juju = jubilant.Juju(model=model, wait_timeout=1200)
    original = juju.status()
    original_machines = set(original.machines)
    app = "alloy-network-test-" + uuid.uuid4().hex[:8]
    assert app not in original.apps
    try:
        juju.deploy(artifact, app=app, base=base)
        yield Deployment(juju, app, artifact, base)
    finally:
        current = juju.status()
        if app in current.apps:
            machines = {u.machine for u in current.apps[app].units.values()} - original_machines
            juju.remove_application(app, force=True)
            juju.wait(lambda status: app not in status.apps)
            for machine in machines:
                status = juju.status()
                used = any(
                    u.machine == machine or u.machine.startswith(machine + "/")
                    for a in status.apps.values()
                    for u in a.units.values()
                )
                if machine and machine in status.machines and not used:
                    juju.cli("remove-machine", "--force", "--no-wait", "--no-prompt", machine)


def test_artifact_runtime_and_network_destination_gate(deployment):
    juju, app = deployment.juju, deployment.app
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
    _wait_status(juju, app, "waiting")
    config = juju.cli("ssh", unit, "--", "sudo", "cat", "/etc/alloy/config.alloy")
    assert 'discovery.http "network_netbox"' not in config
    juju.config(app, {"network-monitoring-enabled": False})
    _wait_status(juju, app, "active")


@pytest.fixture
def backends():
    settings = {
        "remote_write": os.environ.get("TEST_REMOTE_WRITE_PROVIDER", ""),
        "mimir_query": os.environ.get("TEST_MIMIR_QUERY_URL", ""),
        "loki": os.environ.get("TEST_LOKI_PROVIDER", ""),
        "loki_query": os.environ.get("TEST_LOKI_QUERY_URL", ""),
    }
    if not settings["remote_write"] or not settings["mimir_query"]:
        pytest.skip("Set TEST_REMOTE_WRITE_PROVIDER and TEST_MIMIR_QUERY_URL for backend checks")
    assert bool(settings["loki"]) == bool(settings["loki_query"])
    return settings


def _wait_status(juju: jubilant.Juju, app: str, expected: str) -> None:
    juju.wait(
        lambda status: app in status.apps
        and bool(status.apps[app].units)
        and all(u.workload_status.current == expected for u in status.apps[app].units.values())
        and all(u.juju_status.current == "idle" for u in status.apps[app].units.values()),
        error=lambda status: app in status.apps
        and any(
            u.juju_status.current == "error"
            or (
                expected != "blocked"
                and u.juju_status.current == "idle"
                and u.workload_status.current == "blocked"
            )
            for u in status.apps[app].units.values()
        ),
    )


def _assert_topology(samples: list, expected: dict[str, str], *, logs: bool = False) -> None:
    assert samples
    for sample in samples:
        labels = sample["stream" if logs else "metric"]
        assert all(labels.get(key) == value for key, value in expected.items())


@pytest.mark.parametrize("artifact", ["prior"], indirect=True)
def test_live_relations_labels_secrets_and_refresh(backends, deployment, tmp_path):
    """Exercise actual relations/backends using fake inventory and isolated local sources."""
    juju, app = deployment.juju, deployment.app
    _wait_status(juju, app, "active")
    unit = next(iter(juju.status().apps[app].units))
    model_info = json.loads(
        juju.cli("show-model", juju.model, "--format=json", include_model=False)
    )
    model = next(iter(model_info.values()))
    expected = {
        "juju_model": juju.status().model.name,
        "juju_model_uuid": model["model-uuid"],
        "juju_application": app,
        "juju_unit": unit,
        "juju_charm": "alloy-network-monitoring",
    }
    location_labels = {"site": "fixture", "region": "fixture-region"}
    start_source(juju, unit)
    secrets = []
    source_app = "network-source-fixture-" + uuid.uuid4().hex[:8]
    assert source_app not in juju.status().apps
    evidence: dict[str, Any] = {
        "model": juju.model,
        "app": app,
        "unit": unit,
        "base": deployment.base,
        "expected_topology": expected,
        "expected_location_labels": location_labels,
        "started_utc": datetime.now(timezone.utc).isoformat(),
        "prior_artifact_sha256": hashlib.sha256(deployment.artifact.read_bytes()).hexdigest(),
        "snmp_scope": (
            "Loopback has no SNMP agent; diagnostic up=0 verifies labels/delivery, "
            "not interface counters."
        ),
    }
    try:
        auth = (
            "auths:\n  fixture:\n    version: 3\n    username: fixture\n"
            "    security_level: noAuthNoPriv\n"
        )
        token = juju.cli("add-secret", app + "-token", "token=fixture-token-1").strip()
        secrets.append(token)
        snmp = juju.cli("add-secret", app + "-snmp", "snmp-yaml=" + auth).strip()
        secrets.append(snmp)
        for secret in secrets:
            juju.cli("grant-secret", secret, app)
        juju.config(
            app,
            {
                "custom_args": "--server.http.listen-addr=127.0.0.1:6987",
                "network-monitoring-enabled": True,
                "collector-id": app,
                "netbox-url": "http://127.0.0.1:19998",
                "netbox-token": token,
                "snmp-auth": snmp,
                "netbox-refresh-interval": "1s",
                "snmp-scrape-interval": "5s",
                "snmp-scrape-timeout": "2s",
                "netbox-site": "fixture",
                "netbox-region": "fixture-region",
                "netbox-tags": "snmp",
                "manual-metrics-jobs": json.dumps(
                    [
                        {
                            "name": "integration_fixture",
                            "targets": ["127.0.0.1:19998"],
                            "scrape_interval": "5s",
                            "scrape_timeout": "2s",
                        }
                    ]
                ),
                "enable-syslogreceivers": bool(backends["loki"]),
                "journal-match-expressions": "SYSLOG_IDENTIFIER=alloy-network-fixture",
                "systemd-units": "alloy.service",
            },
        )
        _wait_status(juju, app, "waiting")
        juju.integrate(app + ":send-remote-write", backends["remote_write"])
        if backends["loki"]:
            juju.integrate(app + ":send-loki-logs", backends["loki"])
        _wait_status(juju, app, "active")
        before = wait_for(lambda: (s := snapshot(juju, unit)) and s["auth_revision"] == 1 and s)
        current = Path(os.environ["CHARM_PATH"]).resolve()
        evidence["artifact_sha256"] = hashlib.sha256(current.read_bytes()).hexdigest()
        revision = juju.status().apps[app].charm_rev
        juju.refresh(app, path=current)
        juju.wait(lambda status: status.apps[app].charm_rev != revision)
        _wait_status(juju, app, "active")
        after = snapshot(juju, unit)
        assert after["pid"] == before["pid"] != "0"
        assert after["start_time"] == before["start_time"]
        assert after["storage_path"] == before["storage_path"]
        assert before["storage_path"] and before["wal_segments"]
        assert after["wal_path"] == before["wal_path"] and after["wal_segments"]
        evidence.update(before_refresh=before, after_refresh=after)
        juju.config(app, {"log-level": "debug", "systemd-units": ""})
        _wait_status(juju, app, "active")
        assert snapshot(juju, unit)["log_level"] == "debug"
        source_artifact = build_source_fixture(current, tmp_path / "source-fixture.charm")
        machine = juju.status().apps[app].units[unit].machine
        juju.deploy(source_artifact, app=source_app, base=deployment.base, to=machine)
        _wait_status(juju, source_app, "active")
        juju.integrate(source_app + ":machine-observability", app + ":machine-observability")
        _wait_status(juju, app, "active")
        source_unit = next(iter(juju.status().apps[source_app].units))
        provider_topology = {
            **expected,
            "juju_application": source_app,
            "juju_unit": source_unit,
            "juju_charm": "network-source-fixture",
        }
        provider_selector = f'juju_application="{source_app}"'
        provider_samples = wait_for(
            lambda: query(
                backends["mimir_query"], "alloy_provider_fixture_metric{" + provider_selector + "}"
            )
        )
        _assert_topology(provider_samples, {**provider_topology, **location_labels})
        evidence.update(
            source_fixture_artifact_sha256=hashlib.sha256(
                source_artifact.read_bytes()
            ).hexdigest(),
            provider_topology=provider_topology,
            provider_metrics=provider_samples,
        )
        selector = f'juju_application="{app}",site="fixture",region="fixture-region"'
        for metric in ("alloy_network_fixture_metric", "node_memory_MemTotal_bytes"):
            samples = wait_for(
                lambda: query(backends["mimir_query"], metric + "{" + selector + "}")
            )
            _assert_topology(samples, {**expected, **location_labels})
            evidence[metric] = samples
        samples = wait_for(
            lambda: query(backends["mimir_query"], f'up{{job="snmp/network",{selector}}}')
        )
        _assert_topology(samples, {**expected, **location_labels})
        assert samples[0]["metric"]["instance"] == "127.0.0.2"
        assert samples[0]["metric"]["netbox_id"] == "1"
        evidence["snmp_up"] = samples
        _check_network_feature_controls(deployment, backends, evidence)
        if backends["loki"]:
            emit_logs(juju, unit, app + " synthetic log")
            for source in ("remote_syslog", "journal"):
                streams = wait_for(
                    lambda: query(
                        backends["loki_query"],
                        "{" + selector + f',log_source="{source}"' + "}",
                        logs=True,
                    )
                )
                _assert_topology(streams, {**expected, **location_labels}, logs=True)
                evidence[source] = streams
                if source == "remote_syslog":
                    assert streams[0]["stream"]["syslog_hostname"] == "fixture-router"
                    assert streams[0]["stream"]["source_ip"] == "127.0.0.1"
                    indexed = indexed_log_labels(
                        backends["loki_query"],
                        "{" + selector + ',log_source="remote_syslog"}',
                    )
                    assert indexed and all(len(labels) <= 15 for labels in indexed)
                    assert all("syslog_proc_id" not in labels for labels in indexed)
                    assert streams[0]["stream"]["syslog_proc_id"] == "123"
                    assert streams[0]["stream"]["component"] == "loki.source.syslog"
                    evidence["syslog_indexed_labels"] = indexed
            provider_logs = wait_for(
                lambda: query(
                    backends["loki_query"],
                    "{" + provider_selector + ',log_source="provider_fixture"}',
                    logs=True,
                )
            )
            _assert_topology(provider_logs, {**provider_topology, **location_labels}, logs=True)
            evidence["provider_logs"] = provider_logs
        generation = snapshot(juju, unit)
        rotate_source(juju, unit)
        juju.cli("update-secret", token, "token=fixture-token-2")
        juju.cli(
            "update-secret",
            snmp,
            "snmp-yaml=" + auth.replace("  fixture:", "  rotated:"),
        )
        _wait_status(juju, app, "active")
        rotated = wait_for(
            lambda: (s := snapshot(juju, unit))
            and s["auth_revision"] == 2
            and s["snmp_auth_profiles"] == ["rotated"]
            and s
        )
        assert rotated["secret_files"] != generation["secret_files"]
        assert rotated["pid"] == generation["pid"]
        evidence["after_rotation"] = rotated
        v3_generation = rotated
        juju.cli(
            "update-secret",
            snmp,
            "snmp-yaml=auths:\n  rotated:\n    version: 2\n    community: fixture-v2c-community\n",
        )
        _wait_status(juju, app, "active")
        rotated = wait_for(
            lambda: (s := snapshot(juju, unit))
            and s["secret_files"] != v3_generation["secret_files"]
            and s["snmp_auth_profiles"] == ["rotated"]
            and s
        )
        assert rotated["pid"] == v3_generation["pid"]
        assert rotated["storage_path"] == v3_generation["storage_path"]
        evidence["after_v2c_profile_transition"] = rotated
        juju.config(app, {"log-level": "invalid"})
        _wait_status(juju, app, "blocked")
        rejected = snapshot(juju, unit)
        assert rejected["config_sha256"] == rotated["config_sha256"]
        assert rejected["pid"] == rotated["pid"]
        juju.config(app, {"log-level": "critical"})
        _wait_status(juju, app, "active")
        assert snapshot(juju, unit)["log_level"] == "error"
        juju.cli("revoke-secret", snmp, app)
        juju.config(app, {"log-level": "info"})
        _wait_status(juju, app, "blocked")
        withdrawn = snapshot(juju, unit)
        assert not withdrawn["network_present"]
        assert not withdrawn["secret_files"]
        assert withdrawn["pid"] == rotated["pid"]
        evidence["after_withdrawal"] = withdrawn
        assert query(backends["mimir_query"], "alloy_network_fixture_metric{" + selector + "}")
    finally:
        folder = Path(os.environ.get("TEST_EVIDENCE_DIR", "build/live-evidence"))
        folder.mkdir(parents=True, exist_ok=True)
        evidence["finished_utc"] = datetime.now(timezone.utc).isoformat()
        (folder / (app + ".json")).write_text(json.dumps(evidence, indent=2) + "\n")
        for secret in secrets:
            juju.cli("remove-secret", secret)
        if source_app in juju.status().apps:
            juju.remove_application(source_app)
            juju.wait(lambda status: source_app not in status.apps)


def _check_network_feature_controls(deployment, backends, evidence):
    """Verify selected modules, metadata and apply recovery on the owned application."""
    juju, app = deployment.juju, deployment.app
    unit = next(iter(juju.status().apps[app].units))
    initial = snapshot(juju, unit)
    set_inventory_modules(juju, unit, "system,if_mib,cisco_device")
    wait_for(lambda: network_target_modules(juju, unit) == ["system,if_mib,cisco_device"])
    set_inventory_modules(juju, unit, "unknown_module")
    invalid = wait_for(
        lambda: query(
            backends["mimir_query"],
            f'up{{job="snmp/network",juju_application="{app}",'
            'snmp_config_error="invalid_modules"}',
        )
    )
    assert invalid[0]["value"][1] == "0"
    set_inventory_modules(juju, unit, "")
    wait_for(lambda: network_target_modules(juju, unit) == ["system,if_mib"])
    assert snapshot(juju, unit)["pid"] == initial["pid"]
    evidence["module_changes_without_restart"] = True
    if not backends["loki"]:
        return
    juju.config(
        app,
        {
            "syslog-parsers": "opnsense_filterlog,cisco_ios",
            "network-device-alerts-enabled": True,
            "network-log-alerts-enabled": True,
            "network-alerts-config": "interfaces: [{name: expected, include: wan}]",
        },
    )
    _wait_status(juju, app, "active")
    selector = (
        '{juju_application="' + app + '",log_source="remote_syslog",'
        'site="fixture",region="fixture-region"}'
    )
    events = [
        (
            "filterlog",
            "100,,,123,igb0,match,block,in,4,0x0,,64,0,0,none,6,tcp,60,"
            "192.0.2.1,198.51.100.1,12345,443,0,S,,,65535,,",
            'event_vendor="opnsense" | event_dst_port="443"',
        ),
        (
            "ios",
            "%LINK-3-UPDOWN: Interface TenGigabitEthernet1/1, changed state to down",
            'event_vendor="cisco" | event_interface="TenGigabitEthernet1/1"',
        ),
    ]
    for application, message, filters in events:
        emit_logs(juju, unit, message, application=application)
        samples = wait_for(
            lambda: query(backends["loki_query"], selector + " | " + filters, logs=True)
        )
        assert any(value[1] == message for sample in samples for value in sample["values"])
    labels = indexed_log_labels(backends["loki_query"], selector)
    assert labels and all(len(item) <= 15 for item in labels)
    assert all(not any(key.startswith("event_") for key in item) for item in labels)
    applied = snapshot(juju, unit)
    assert applied["pid"] == initial["pid"]
    juju.config(app, {"network-alerts-enabled": False, "network-log-alerts-enabled": False})
    _wait_status(juju, app, "active")
    disabled = snapshot(juju, unit)
    assert disabled["config_sha256"] == applied["config_sha256"]
    assert disabled["pid"] == applied["pid"]
    juju.config(app, {"syslog-parsers": "unknown"})
    _wait_status(juju, app, "blocked")
    rejected = snapshot(juju, unit)
    assert rejected["config_sha256"] == disabled["config_sha256"]
    juju.config(
        app,
        {
            "syslog-parsers": "",
            "network-alerts-enabled": True,
            "network-device-alerts-enabled": False,
            "network-alerts-config": "",
        },
    )
    _wait_status(juju, app, "active")
    assert snapshot(juju, unit)["pid"] == initial["pid"]
    evidence["parser_events"] = [application for application, _, _ in events]
    evidence["parser_indexed_labels"] = labels
    evidence["alert_flags_leave_collection_unchanged"] = True
    evidence["invalid_parser_retains_applied_config"] = True
