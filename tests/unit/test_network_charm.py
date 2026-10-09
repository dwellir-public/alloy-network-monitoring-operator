import json
import subprocess
from dataclasses import replace
from pathlib import Path

import pytest
from ops import testing

import alloy
import charm
import secret_files
from charm import AlloyCharm

AUTH = """auths:
  first:
    version: 3
    username: monitor
    security_level: authPriv
    auth_protocol: SHA256
    password: fake-auth-password
    priv_protocol: AES
    priv_password: fake-private-password
  second:
    version: 3
    username: another
    security_level: noAuthNoPriv
"""


@pytest.fixture
def runtime(tmp_path, monkeypatch):
    config_path = tmp_path / "config.alloy"
    defaults_path = tmp_path / "defaults"
    defaults_path.write_text('STORAGE_PATH="/var/lib/alloy"\n')
    monkeypatch.setattr(charm, "DEFAULT_CONFIG_PATH", str(config_path))
    monkeypatch.setattr(charm, "DEFAULT_CONFIG_BACKUP_PATH", str(tmp_path / "config.bak"))
    monkeypatch.setattr(alloy, "DEFAULT_CONFIG_PATH", str(config_path))
    monkeypatch.setattr(alloy, "DEFAULT_SYSTEMD_DEFAULTS_PATH", str(defaults_path))
    monkeypatch.setattr(secret_files, "DEFAULT_SECRET_DIR", tmp_path / "secrets")
    monkeypatch.setattr(alloy, "verify_config", lambda **_: None)
    monkeypatch.setattr(alloy, "ensure_config_dir_permissions", lambda *_: None)
    monkeypatch.setattr(alloy, "is_active", lambda: True)
    monkeypatch.setattr(alloy, "get_version", lambda: "1.20.1")
    calls = []
    monkeypatch.setattr(alloy, "restart", lambda: calls.append("restart"))
    monkeypatch.setattr(alloy, "reload", lambda **_: calls.append("reload"))
    monkeypatch.setattr(alloy, "wait_ready", lambda *_: None)
    monkeypatch.setattr(alloy, "stop", lambda: calls.append("stop"))
    monkeypatch.setattr(AlloyCharm, "_remote_write_endpoint_urls", lambda _: ["http://mimir/push"])
    return testing.Context(AlloyCharm), calls, config_path, defaults_path


def initial_state(**config):
    token = testing.Secret({"token": "fake-netbox-token"})
    snmp = testing.Secret({"snmp-yaml": AUTH})
    return testing.State(
        config={
            "network-monitoring-enabled": True,
            "netbox-url": "https://netbox.example",
            "collector-id": "lab",
            "netbox-token": token.id,
            "snmp-auth": snmp.id,
            **config,
        },
        secrets=[token, snmp],
    )


def applied(state):
    return next(
        item.content
        for item in state.stored_states
        if item.name == "_stored" and item.owner_path == "AlloyCharm"
    )


def test_secrets_and_multiple_profiles_never_enter_config_or_state(runtime, caplog):
    ctx, calls, config_path, defaults_path = runtime
    output = ctx.run(ctx.on.config_changed(), initial_state())
    assert isinstance(output.unit_status, testing.ActiveStatus)
    text = config_path.read_text()
    assert "(first|second)" in text
    assert 'replacement = "snmp/network"' in text
    assert 'STORAGE_PATH="/var/lib/alloy"' in defaults_path.read_text()
    assert calls == ["restart"]
    for secret in ("fake-netbox-token", "fake-auth-password", "fake-private-password"):
        assert secret not in text
        assert secret not in json.dumps(dict(applied(output)))
        assert secret not in caplog.text


def test_secret_rotation_applies_once_and_removes_old_generation(runtime):
    ctx, calls, config_path, _ = runtime
    first = ctx.run(ctx.on.config_changed(), initial_state())
    previous = dict(applied(first)["network_secret_files"])
    old_token = next(item for item in first.secrets if "token" in item.tracked_content)
    rotated = replace(old_token, latest_content={"token": "fake-rotated-token"})
    state = replace(
        first, secrets=[rotated, *(item for item in first.secrets if item.id != old_token.id)]
    )
    output = ctx.run(ctx.on.secret_changed(rotated), state)
    current = dict(applied(output)["network_secret_files"])
    assert Path(current["token"]).read_text() == "fake-rotated-token"
    assert not Path(previous["token"]).exists()
    assert calls == ["restart", "reload"]
    text = config_path.read_text()
    unchanged = ctx.run(ctx.on.update_status(), output)
    assert calls == ["restart", "reload"]
    assert config_path.read_text() == text
    assert applied(unchanged)["last_good_config"] == text


def test_unavailable_secret_withdraws_network_but_keeps_local_metrics(runtime):
    ctx, calls, config_path, _ = runtime
    first = ctx.run(ctx.on.config_changed(), initial_state())
    previous = dict(applied(first)["network_secret_files"])
    output = ctx.run(ctx.on.update_status(), replace(first, secrets=[]))
    assert isinstance(output.unit_status, testing.BlockedStatus)
    assert "unavailable" in output.unit_status.message
    assert 'discovery.http "network_netbox"' not in config_path.read_text()
    assert 'prometheus.scrape "default"' in config_path.read_text()
    assert applied(output)["network_secret_files"] == {}
    assert not Path(previous["token"]).exists()
    assert calls == ["restart", "reload"]


def test_invalid_auth_rotation_preserves_working_generation(runtime, caplog):
    ctx, calls, config_path, _ = runtime
    first = ctx.run(ctx.on.config_changed(), initial_state())
    old = next(item for item in first.secrets if "snmp-yaml" in item.tracked_content)
    invalid = replace(old, latest_content={"snmp-yaml": "fake-secret-invalid: ["})
    state = replace(
        first, secrets=[invalid, *(item for item in first.secrets if item.id != old.id)]
    )
    output = ctx.run(ctx.on.secret_changed(invalid), state)
    assert isinstance(output.unit_status, testing.BlockedStatus)
    assert applied(output)["last_good_config"] == applied(first)["last_good_config"]
    assert config_path.read_text() == applied(first)["last_good_config"]
    assert calls == ["restart"]
    assert "fake-secret-invalid" not in caplog.text


def test_failed_reload_restores_files_state_and_can_retry(runtime, monkeypatch):
    ctx, calls, config_path, defaults_path = runtime
    first = ctx.run(ctx.on.config_changed(), initial_state())
    previous_config = config_path.read_text()
    previous_defaults = defaults_path.read_text()
    old = next(item for item in first.secrets if "token" in item.tracked_content)
    rotated = replace(old, latest_content={"token": "fake-rotated-token"})
    state = replace(
        first, secrets=[rotated, *(item for item in first.secrets if item.id != old.id)]
    )

    def fail_first_reload(**_):
        calls.append("reload")
        if calls.count("reload") == 1:
            raise subprocess.CalledProcessError(1, ["systemctl", "reload", "alloy"])

    monkeypatch.setattr(alloy, "reload", fail_first_reload)
    output = ctx.run(ctx.on.secret_changed(rotated), state)
    assert isinstance(output.unit_status, testing.BlockedStatus)
    assert config_path.read_text() == previous_config
    assert defaults_path.read_text() == previous_defaults
    assert applied(output)["network_secret_files"] == applied(first)["network_secret_files"]
    assert len(list(secret_files.DEFAULT_SECRET_DIR.glob("generation-*"))) == 1
    retry = ctx.run(ctx.on.config_changed(), output)
    assert isinstance(retry.unit_status, testing.ActiveStatus)
    assert applied(retry)["last_good_config"] != previous_config
    assert calls == ["restart", "reload", "reload", "reload"]


@pytest.mark.parametrize(
    "config",
    [{"network-monitoring-enabled": False}, {"config-override": 'logging {\n level = "info"\n}'}],
)
def test_disabled_and_override_do_not_require_secrets(runtime, config):
    ctx, _, config_path, _ = runtime
    output = ctx.run(ctx.on.config_changed(), replace(initial_state(**config), secrets=[]))
    assert isinstance(output.unit_status, testing.ActiveStatus)
    assert 'local.file "network_netbox_token"' not in config_path.read_text()


def test_missing_destination_waits_without_staging_credentials(runtime, monkeypatch):
    ctx, _, config_path, _ = runtime
    monkeypatch.setattr(AlloyCharm, "_remote_write_endpoint_urls", lambda _: [])
    output = ctx.run(ctx.on.config_changed(), initial_state())
    assert isinstance(output.unit_status, testing.WaitingStatus)
    assert 'discovery.http "network_netbox"' not in config_path.read_text()
    assert not secret_files.DEFAULT_SECRET_DIR.exists()


def test_one_active_collector_per_application(runtime):
    ctx, _, config_path, _ = runtime
    output = ctx.run(ctx.on.config_changed(), replace(initial_state(), planned_units=2))
    assert isinstance(output.unit_status, testing.BlockedStatus)
    assert 'discovery.http "network_netbox"' not in config_path.read_text()


def test_snmp_profile_rotation_is_applied_and_old_files_retired(runtime):
    ctx, calls, _, _ = runtime
    first = ctx.run(ctx.on.config_changed(), initial_state())
    old_files = dict(applied(first)["network_secret_files"])
    old = next(item for item in first.secrets if "snmp-yaml" in item.tracked_content)
    rotated = replace(
        old,
        latest_content={"snmp-yaml": AUTH.replace("fake-auth-password", "new-fake-auth-password")},
    )
    state = replace(
        first, secrets=[rotated, *(item for item in first.secrets if item.id != old.id)]
    )
    output = ctx.run(ctx.on.secret_changed(rotated), state)
    current = dict(applied(output)["network_secret_files"])
    assert "new-fake-auth-password" in Path(current["snmp"]).read_text()
    assert not Path(old_files["snmp"]).exists()
    assert calls == ["restart", "reload"]


def test_network_alerts_use_existing_payload_and_device_scope(runtime):
    ctx, _, _, _ = runtime
    relation = testing.Relation("send-remote-write", remote_app_name="mimir")
    output = ctx.run(
        ctx.on.config_changed(), replace(initial_state(), leader=True, relations=[relation])
    )
    data = next(item for item in output.relations if item.id == relation.id).local_app_data
    payload = json.loads(data["alert_rules"])
    assert set(payload) == {"groups"}
    rules = payload["groups"][0]["rules"]
    assert {rule["alert"] for rule in rules} == {
        "SNMPDeviceDown",
        "SNMPNoEligibleTargets",
        "NetBoxDiscoveryFailing",
        "NetworkCollectorDown",
        "NetworkRemoteWriteBacklog",
        "NetworkCollectorDiskLow",
    }
    assert all(rule["labels"]["collector"] == "lab" for rule in rules)
    assert "juju_application" not in json.dumps(rules)


def test_override_preserves_referenced_managed_files(runtime):
    ctx, _, config_path, _ = runtime
    first = ctx.run(ctx.on.config_changed(), initial_state())
    previous = dict(applied(first)["network_secret_files"])
    overridden = replace(
        first,
        config={**first.config, "config-override": config_path.read_text() + "\n// override\n"},
    )
    output = ctx.run(ctx.on.config_changed(), overridden)
    assert applied(output)["network_secret_files"] == previous
    assert all(Path(path).exists() for path in previous.values())


def test_failed_withdrawal_stops_collector_after_access_loss(runtime, monkeypatch):
    ctx, calls, _, _ = runtime
    first = ctx.run(ctx.on.config_changed(), initial_state())
    monkeypatch.setattr(AlloyCharm, "_validate_config_text", lambda *_: False)
    output = ctx.run(ctx.on.update_status(), replace(first, secrets=[]))
    assert isinstance(output.unit_status, testing.BlockedStatus)
    assert calls == ["restart", "stop"]
    assert applied(output)["last_good_config"] == applied(first)["last_good_config"]


def test_refresh_preserves_running_generation_with_pending_secret_revision(runtime):
    ctx, calls, config_path, _ = runtime
    first = ctx.run(ctx.on.config_changed(), initial_state())
    previous_config = config_path.read_text()
    old = next(item for item in first.secrets if "token" in item.tracked_content)
    rotated = replace(old, latest_content={"token": "pending-fake-token"})
    state = replace(
        first, secrets=[rotated, *(item for item in first.secrets if item.id != old.id)]
    )
    output = ctx.run(ctx.on.upgrade_charm(), state)
    assert output.unit_status == first.unit_status
    assert config_path.read_text() == previous_config
    assert applied(output)["last_good_config"] == applied(first)["last_good_config"]
    assert calls == ["restart"]
