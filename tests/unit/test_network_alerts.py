import json
from typing import Any, cast

import pytest
import yaml

from network_alert_settings import NetworkAlertsError, parse_alert_settings
from network_alerts import device_alert_groups, network_log_alert_groups
from network_syslog import SyslogParsersError, parse_syslog_parsers, render_syslog_parser_stages
from snmp_modules import SnmpModulesError, parse_module_list

TOPOLOGY = {
    "juju_model_uuid": "model-id",
    "juju_application": "collector",
    "juju_unit": "collector/0",
}


def rules_from_groups(groups: list[dict[str, object]]) -> list[dict[str, Any]]:
    return [rule for group in groups for rule in cast(list[dict[str, Any]], group["rules"])]


@pytest.mark.parametrize("value", ["unknown", "system,", "", ",system", "system," * 9])
def test_module_settings_reject_unknown_and_malformed_lists(value):
    with pytest.raises(SnmpModulesError):
        parse_module_list(value)


def test_module_settings_normalize_public_config_and_preserve_order():
    assert parse_module_list(" system, if_mib, cisco_device, system ") == (
        "system",
        "if_mib",
        "cisco_device",
    )


@pytest.mark.parametrize(
    "data",
    [
        {"unknown": True},
        {"cpu": {"threshold": float("nan")}},
        {"cpu": {"threshold": 101}},
        {"cpu": {"threshold": True}},
        {"cpu": {"for": "2d"}},
        {"cpu": {"for": "1m1h"}},
        {"cpu": {"for": "9" * 5000 + "s"}},
        {"logs": {"unknown": {}}},
        {"logs": {"link_changes": {"window": "0s"}}},
        {"logs": {"link_changes": {"count": 2.5}}},
        {"interfaces": [{"name": "wan", "include": "(?=unsafe)"}]},
        {"interfaces": [{"name": "wan", "include": "["}]},
        {"interfaces": [{"name": "wan", "include": "a++"}]},
        {"interfaces": [{"name": "wan", "include": "a{1001}"}]},
        {"interfaces": [{"name": "wan", "include": r"a\Z"}]},
        {"interfaces": [{"name": "wan", "include": r"\N{LATIN CAPITAL LETTER A}"}]},
        {"interfaces": [{"name": "wan", "include": r"\u0041"}]},
        {"interfaces": [{"name": "wan", "include": "foo", "field": "unknown"}]},
        {"interfaces": [{"name": "wan", "include": "a"}, {"name": "wan", "include": "b"}]},
    ],
)
def test_alert_config_rejects_invalid_schema_without_echoing_values(data):
    with pytest.raises(NetworkAlertsError) as error:
        parse_alert_settings({"network-alerts-config": yaml.safe_dump(data)})
    assert "unsafe" not in str(error.value)


def test_duplicate_yaml_keys_and_oversized_config_are_rejected():
    for value in ("cpu: {}\ncpu: {}", "x" * (16 * 1024 + 1)):
        with pytest.raises(NetworkAlertsError):
            parse_alert_settings({"network-alerts-config": value})


def test_new_alerts_and_parsing_are_opt_in_and_controls_are_independent():
    settings = parse_alert_settings({})
    assert settings.metrics_enabled and not settings.devices_enabled and not settings.logs_enabled
    assert not settings.parsers
    assert not device_alert_groups("lab", "owner", topology_labels=TOPOLOGY, settings=settings)
    assert not network_log_alert_groups("owner", topology_labels=TOPOLOGY, settings=settings)
    settings = parse_alert_settings(
        {
            "network-alerts-enabled": False,
            "network-device-alerts-enabled": True,
            "network-log-alerts-enabled": True,
            "syslog-parsers": "cisco_ios",
        }
    )
    assert not device_alert_groups("lab", "owner", topology_labels=TOPOLOGY, settings=settings)
    assert network_log_alert_groups("owner", topology_labels=TOPOLOGY, settings=settings)


def test_interface_scope_and_safe_health_rules_use_metric_semantics():
    settings = parse_alert_settings(
        {
            "network-device-alerts-enabled": True,
            "network-alerts-config": (
                "interfaces:\n- name: uplinks\n  devices: core-.*\n  include: TenGig.*\n"
                "  exclude: TenGig0\nutilisation:\n  threshold: 80\n"
            ),
        }
    )
    groups = device_alert_groups("lab", "owner", topology_labels=TOPOLOGY, settings=settings)
    assert len(groups) == 2
    rules = {r["alert"]: r for r in rules_from_groups(groups)}
    assert "ifAdminStatus" in rules["NetworkInterfaceDown"]["expr"]
    assert 'ifName!~"TenGig0"' in rules["NetworkInterfaceDown"]["expr"]
    assert "* 1000000" in rules["NetworkInterfaceInUtilisationHigh"]["expr"]
    assert " > 80" in rules["NetworkInterfaceInUtilisationHigh"]["expr"]
    assert "clamp_min" in rules["NetworkInterfaceInErrorsHigh"]["expr"]
    assert " >= 10" in rules["NetworkInterfaceInErrorsHigh"]["expr"]
    assert "offAdmin" not in rules["NetworkPowerFault"]["expr"]
    assert rules["NetworkSensorFault"]["expr"].endswith("== 3")
    assert "4294877296" in rules["NetworkSNMPUptimeReset"]["expr"]
    assert "unless" in rules["NetworkMemoryHigh"]["expr"]
    for rule in rules.values():
        assert 'juju_model_uuid="model-id"' in rule["expr"]
        assert rule["labels"]["juju_application"] == "collector"


def test_ports_require_explicit_scope_and_disabled_presets_withdraw_rules():
    settings = parse_alert_settings(
        {
            "network-device-alerts-enabled": True,
            "network-alerts-config": "cpu: {enabled: false}\npower: {enabled: false}",
        }
    )
    groups = device_alert_groups("lab", "owner", topology_labels=TOPOLOGY, settings=settings)
    names = {r["alert"] for r in rules_from_groups(groups)}
    assert "NetworkCPUHigh" not in names and "NetworkPowerFault" not in names
    assert "NetworkInterfaceDown" not in names


def test_log_rules_require_matching_parser_and_preserve_optional_event_defaults():
    settings = parse_alert_settings(
        {"network-log-alerts-enabled": True, "syslog-parsers": "opnsense_filterlog"}
    )
    assert not network_log_alert_groups("owner", topology_labels=TOPOLOGY, settings=settings)
    settings = parse_alert_settings(
        {
            "network-log-alerts-enabled": True,
            "syslog-parsers": "opnsense_filterlog,cisco_ios",
            "network-alerts-config": "logs:\n  firewall_blocks: {enabled: true, count: 20}\n",
        }
    )
    rules = rules_from_groups(
        network_log_alert_groups("owner", topology_labels=TOPOLOGY, settings=settings)
    )
    assert len(rules) == 4
    assert all('log_source="remote_syslog"' in r["expr"] for r in rules)
    assert any(" >= 20" in r["expr"] for r in rules)
    assert not any(r["alert"] == "NetworkConfigurationChanged" for r in rules)


@pytest.mark.parametrize(
    "value", ["unknown", "cisco_ios,", 123, "opnsense_filterlog,cisco_ios,cisco_ios"]
)
def test_parser_profiles_reject_invalid_selection(value):
    with pytest.raises(SyslogParsersError):
        parse_syslog_parsers(value)


def test_parsers_preserve_original_line_and_do_not_add_indexed_labels():
    assert render_syslog_parser_stages(()) == []
    text = "\n".join(
        render_syslog_parser_stages(parse_syslog_parsers("cisco_ios,opnsense_filterlog"))
    )
    assert "stage.structured_metadata" in text
    assert "stage.output" not in text and "stage.labels" not in text
    assert "event_src_ip" in text and "event_mnemonic" in text
    assert "{{ if" in text
    json.dumps(text)
