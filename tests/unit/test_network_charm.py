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
    monkeypatch.setattr(charm, "read_snmp_module_errors", lambda _: ())
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


def rule_payload(state, endpoint):
    relation = next(item for item in state.relations if item.endpoint == endpoint)
    return json.loads(relation.local_app_data["alert_rules"])["groups"]


def test_optional_owned_metric_and_log_rules_withdraw_without_changing_collection(
    runtime, monkeypatch
):
    ctx, calls, config_path, _ = runtime
    monkeypatch.setattr(AlloyCharm, "_loki_endpoint_urls", lambda _: ["http://loki/push"])
    provider_metrics = [
        {"name": "provider-metric", "rules": [{"alert": "ProviderDown", "expr": "up == 0"}]}
    ]
    provider_logs = [
        {
            "name": "provider-log",
            "rules": [
                {
                    "alert": "ProviderEvent",
                    "expr": 'sum(count_over_time({job="provider"}[5m])) > 0',
                }
            ],
        }
    ]
    monkeypatch.setattr(
        AlloyCharm, "_flatten_rule_state", lambda *_: (provider_metrics, provider_logs)
    )
    state = replace(
        initial_state(
            **{
                "enable-syslogreceivers": True,
                "syslog-parsers": "cisco_ios",
                "network-device-alerts-enabled": True,
                "network-log-alerts-enabled": True,
            }
        ),
        leader=True,
        relations=[testing.Relation("send-remote-write"), testing.Relation("send-loki-logs")],
    )
    first = ctx.run(ctx.on.config_changed(), state)
    assert len(rule_payload(first, "send-remote-write")) == 3
    assert len(rule_payload(first, "send-loki-logs")) == 2
    running_config = config_path.read_text()
    disabled = replace(
        first,
        config={
            **first.config,
            "network-alerts-enabled": False,
            "network-log-alerts-enabled": False,
        },
    )
    result = ctx.run(ctx.on.config_changed(), disabled)
    assert rule_payload(result, "send-remote-write") == provider_metrics
    assert rule_payload(result, "send-loki-logs") == provider_logs
    assert config_path.read_text() == running_config
    assert calls == ["restart"]


def test_oversized_interface_presets_keep_previous_config_and_owned_rules(runtime):
    ctx, calls, config_path, _ = runtime
    first = ctx.run(
        ctx.on.config_changed(),
        replace(initial_state(), leader=True, relations=[testing.Relation("send-remote-write")]),
    )
    previous = config_path.read_text()
    scopes = [{"name": f"scope-{i}", "include": ".+"} for i in range(8)]
    result = ctx.run(
        ctx.on.config_changed(),
        replace(
            first,
            config={
                **first.config,
                "network-device-alerts-enabled": True,
                "network-alerts-config": json.dumps({"interfaces": scopes}),
            },
        ),
    )
    assert isinstance(result.unit_status, testing.BlockedStatus)
    assert "capacity" in result.unit_status.message
    assert config_path.read_text() == previous
    assert rule_payload(result, "send-remote-write") == rule_payload(first, "send-remote-write")
    assert calls == ["restart"]


def test_secret_revocation_stops_collector_even_with_invalid_parser_candidate(runtime):
    ctx, calls, _, _ = runtime
    first = ctx.run(ctx.on.config_changed(), initial_state())
    result = ctx.run(
        ctx.on.update_status(),
        replace(first, secrets=[], config={**first.config, "syslog-parsers": "unknown"}),
    )
    assert isinstance(result.unit_status, testing.BlockedStatus)
    assert calls == ["restart", "stop"]


def test_invalid_parser_or_alert_candidate_retains_loaded_config_and_published_rules(
    runtime, monkeypatch
):
    ctx, calls, config_path, _ = runtime
    monkeypatch.setattr(AlloyCharm, "_loki_endpoint_urls", lambda _: ["http://loki/push"])
    state = replace(
        initial_state(
            **{
                "enable-syslogreceivers": True,
                "syslog-parsers": "cisco_ios",
                "network-log-alerts-enabled": True,
            }
        ),
        leader=True,
        relations=[testing.Relation("send-loki-logs"), testing.Relation("send-remote-write")],
    )
    first = ctx.run(ctx.on.config_changed(), state)
    rules = rule_payload(first, "send-loki-logs")
    text = config_path.read_text()
    for update in ({"syslog-parsers": "unsupported"}, {"network-alerts-config": "logs: ["}):
        result = ctx.run(
            ctx.on.config_changed(), replace(first, config={**first.config, **update})
        )
        assert isinstance(result.unit_status, testing.BlockedStatus)
        assert config_path.read_text() == text
        assert rule_payload(result, "send-loki-logs") == rules
    assert calls == ["restart"]


def test_parser_disable_reloads_receiver_and_withdraws_only_its_owned_rules(runtime, monkeypatch):
    ctx, calls, config_path, _ = runtime
    monkeypatch.setattr(AlloyCharm, "_loki_endpoint_urls", lambda _: ["http://loki/push"])
    first = ctx.run(
        ctx.on.config_changed(),
        replace(
            initial_state(
                **{
                    "enable-syslogreceivers": True,
                    "syslog-parsers": "cisco_ios",
                    "network-log-alerts-enabled": True,
                }
            ),
            leader=True,
            relations=[testing.Relation("send-loki-logs"), testing.Relation("send-remote-write")],
        ),
    )
    output = ctx.run(
        ctx.on.config_changed(), replace(first, config={**first.config, "syslog-parsers": ""})
    )
    assert rule_payload(output, "send-loki-logs") == []
    assert rule_payload(output, "send-remote-write")
    assert 'loki.source.syslog "receiver"' in config_path.read_text()
    assert "event_vendor" not in config_path.read_text()
    assert calls == ["restart", "reload"]


def test_log_rules_can_ship_without_netbox_collection_or_metric_alerts(runtime, monkeypatch):
    ctx, _, config_path, _ = runtime
    monkeypatch.setattr(AlloyCharm, "_loki_endpoint_urls", lambda _: ["http://loki/push"])
    state = testing.State(
        config={
            "network-monitoring-enabled": False,
            "network-alerts-enabled": False,
            "enable-syslogreceivers": True,
            "syslog-parsers": "cisco_ios",
            "network-log-alerts-enabled": True,
        },
        leader=True,
        relations=[testing.Relation("send-loki-logs"), testing.Relation("send-remote-write")],
    )
    result = ctx.run(ctx.on.config_changed(), state)
    assert isinstance(result.unit_status, testing.ActiveStatus)
    assert rule_payload(result, "send-loki-logs")
    assert not rule_payload(result, "send-remote-write")
    assert "event_vendor" in config_path.read_text()
    assert 'discovery.http "network_netbox"' not in config_path.read_text()


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
    assert 'target_label = "collector"' in config_path.read_text()
    assert 'replacement = "lab"' in config_path.read_text()
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


@pytest.mark.parametrize(
    ("requested", "expected"),
    [
        ("debug", "debug"),
        ("info", "info"),
        ("warn", "warn"),
        ("warning", "warn"),
        ("error", "error"),
        ("critical", "error"),
    ],
)
def test_log_level_applies_by_reload_and_invalid_change_retains_generation(
    runtime, requested, expected
):
    ctx, calls, config_path, _ = runtime
    first = ctx.run(ctx.on.config_changed(), initial_state(**{"log-level": "debug"}))
    output = ctx.run(
        ctx.on.config_changed(), replace(first, config={**first.config, "log-level": requested})
    )
    assert isinstance(output.unit_status, testing.ActiveStatus)
    assert f'level = "{expected}"' in config_path.read_text()
    previous = config_path.read_text()
    count = len(calls)
    rejected = ctx.run(
        ctx.on.config_changed(), replace(output, config={**output.config, "log-level": "invalid"})
    )
    assert isinstance(rejected.unit_status, testing.BlockedStatus)
    assert rejected.unit_status.message.startswith("log-level must be")
    assert config_path.read_text() == previous
    assert applied(rejected)["last_good_config"] == applied(output)["last_good_config"]
    assert len(calls) == count
    assert calls == (["restart"] if expected == "debug" else ["restart", "reload"])


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


def test_invalid_global_modules_are_blocked_before_apply_and_recover(runtime):
    ctx, calls, config_path, _ = runtime
    first = ctx.run(ctx.on.config_changed(), initial_state())
    previous = config_path.read_text()
    invalid = ctx.run(
        ctx.on.config_changed(),
        replace(first, config={**first.config, "snmp-modules": "system,unknown_module"}),
    )
    assert isinstance(invalid.unit_status, testing.BlockedStatus)
    assert "snmp-modules" in invalid.unit_status.message
    assert config_path.read_text() == previous
    assert applied(invalid)["network_secret_files"] == applied(first)["network_secret_files"]
    assert calls == ["restart"]
    corrected = ctx.run(
        ctx.on.config_changed(),
        replace(invalid, config={**invalid.config, "snmp-modules": "system,if_mib,cisco_device"}),
    )
    assert isinstance(corrected.unit_status, testing.ActiveStatus)
    assert 'replacement = "system,if_mib,cisco_device"' in config_path.read_text()
    assert calls == ["restart", "reload"]


def test_discovered_invalid_modules_report_blocked_and_recover_without_reload(
    runtime, monkeypatch
):
    ctx, calls, config_path, _ = runtime
    first = ctx.run(ctx.on.config_changed(), initial_state())
    previous = config_path.read_text()
    monkeypatch.setattr(charm, "read_snmp_module_errors", lambda _: ("42", "77"))
    invalid = ctx.run(ctx.on.update_status(), first)
    assert isinstance(invalid.unit_status, testing.BlockedStatus)
    assert "snmp_modules" in invalid.unit_status.message
    assert "42, 77" in invalid.unit_status.message
    assert config_path.read_text() == previous
    assert applied(invalid)["network_secret_files"] == applied(first)["network_secret_files"]
    monkeypatch.setattr(charm, "read_snmp_module_errors", lambda _: ())
    recovered = ctx.run(ctx.on.update_status(), invalid)
    assert isinstance(recovered.unit_status, testing.ActiveStatus)
    assert calls == ["restart"]


def test_module_status_api_failure_waits_without_changing_collection(runtime, monkeypatch):
    ctx, calls, config_path, _ = runtime
    first = ctx.run(ctx.on.config_changed(), initial_state())
    previous = config_path.read_text()
    monkeypatch.setattr(charm, "read_snmp_module_errors", lambda _: None)
    output = ctx.run(ctx.on.update_status(), first)
    assert isinstance(output.unit_status, testing.WaitingStatus)
    assert "local Alloy API" in output.unit_status.message
    assert config_path.read_text() == previous
    assert calls == ["restart"]


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


def test_v2c_community_rotation_stays_protected_and_reloads(runtime, caplog):
    ctx, calls, config_path, _ = runtime
    state = initial_state()
    old = next(item for item in state.secrets if "snmp-yaml" in item.tracked_content)
    community = "fake-v2c-community-1"
    raw = AUTH + f'  legacy_v2c:\n    version: 2\n    community: "{community}"\n'
    mixed = replace(old, tracked_content={"snmp-yaml": raw}, latest_content={"snmp-yaml": raw})
    state = replace(state, secrets=[mixed, *(s for s in state.secrets if s.id != old.id)])
    first = ctx.run(ctx.on.config_changed(), state)
    assert isinstance(first.unit_status, testing.ActiveStatus)
    previous = dict(applied(first)["network_secret_files"])
    assert community in Path(previous["snmp"]).read_text()
    rotated = replace(
        mixed, latest_content={"snmp-yaml": raw.replace(community, "fake-v2c-community-2")}
    )
    result = ctx.run(
        ctx.on.secret_changed(rotated),
        replace(first, secrets=[rotated, *(s for s in first.secrets if s.id != mixed.id)]),
    )
    assert isinstance(result.unit_status, testing.ActiveStatus)
    current = dict(applied(result)["network_secret_files"])
    assert "fake-v2c-community-2" in Path(current["snmp"]).read_text()
    assert not Path(previous["snmp"]).exists()
    assert calls == ["restart", "reload"]
    for secret in (community, "fake-v2c-community-2"):
        assert secret not in config_path.read_text()
        assert secret not in json.dumps(dict(applied(result)))
        assert secret not in caplog.text


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
    assert all('juju_application="alloy-network-monitoring"' in rule["expr"] for rule in rules)
    assert all(rule["labels"]["juju_application"] == "alloy-network-monitoring" for rule in rules)


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
