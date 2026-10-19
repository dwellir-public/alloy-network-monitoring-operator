import io
import json
from unittest.mock import Mock
from urllib.error import URLError
from urllib.parse import parse_qs, urlsplit

import pytest
import yaml

import network_monitoring
from config_builder import ConfigBuilder
from network_monitoring import (
    NetworkMonitoringError,
    NetworkPipeline,
    parse_location_labels,
    parse_netbox_token,
    parse_network_settings,
    parse_snmp_authentications,
    read_snmp_module_errors,
)

AUTH_YAML = """auths:
  opnsense:
    version: 3
    username: monitoring
    security_level: authPriv
    password: test-auth-password
    auth_protocol: SHA256
    priv_protocol: AES
    priv_password: test-privacy-password
  cisco:
    version: 3
    username: switches
    security_level: authNoPriv
    password: test-cisco-password
    auth_protocol: SHA
"""


def settings(**overrides):
    resolved = parse_network_settings(
        {
            "network-monitoring-enabled": True,
            "netbox-url": "https://netbox.example/prefix/",
            "collector-id": "lab",
            **overrides,
        }
    )
    assert resolved is not None
    return resolved


@pytest.mark.parametrize(
    "filters",
    [
        {},
        {"netbox-site": "home"},
        {"netbox-region": "sweden"},
        {"netbox-tags": "snmp, edge"},
        {"netbox-site": "home", "netbox-region": "sweden", "netbox-tags": "snmp,edge,snmp"},
    ],
)
def test_optional_filters(filters):
    resolved = settings(**filters)
    url = urlsplit(resolved.discovery_url)
    query = parse_qs(url.query)
    assert url.path == "/prefix/api/plugins/prometheus-sd/devices/"
    assert query == {
        "status": ["active"],
        **({"site": [filters["netbox-site"]]} if filters.get("netbox-site") else {}),
        **({"region": [filters["netbox-region"]]} if filters.get("netbox-region") else {}),
        **({"tag": ["edge", "snmp"]} if filters.get("netbox-tags") else {}),
    }


def test_disabled_override_and_empty_status():
    assert parse_network_settings({}) is None
    assert (
        parse_network_settings({"network-monitoring-enabled": True, "config-override": "x"})
        is None
    )
    assert not urlsplit(settings(**{"netbox-status": ""}).discovery_url).query


@pytest.mark.parametrize(
    "config, expected",
    [
        ({}, {}),
        ({"netbox-site": "", "netbox-region": ""}, {}),
        ({"netbox-site": "site-one"}, {"site": "site-one"}),
        ({"netbox-region": "region-one"}, {"region": "region-one"}),
        (
            {"netbox-site": " site-one ", "netbox-region": "region-one"},
            {"site": "site-one", "region": "region-one"},
        ),
    ],
)
def test_location_defaults_do_not_require_network_monitoring(config, expected):
    assert parse_location_labels(config) == expected


@pytest.mark.parametrize("key", ["netbox-site", "netbox-region"])
def test_location_defaults_reject_invalid_slugs_without_echoing_input(key):
    with pytest.raises(NetworkMonitoringError, match=f"{key} must be a NetBox slug") as error:
        parse_location_labels({key: "invalid/private-value"})
    assert "private-value" not in str(error.value)


@pytest.mark.parametrize(
    "overrides",
    [
        {"netbox-url": "https://secret@netbox.example"},
        {"netbox-url": "file:///etc/passwd"},
        {"netbox-url": "https://netbox.example?token=private"},
        {"netbox-site": "home&tag=other"},
        {"netbox-tags": "snmp|other"},
        {"collector-id": ""},
        {"snmp-scrape-interval": "0s"},
        {"snmp-scrape-timeout": "30s"},
        {"netbox-refresh-interval": "wrong"},
        {"netbox-auth-scheme": "Basic"},
    ],
)
def test_invalid_settings(overrides):
    with pytest.raises(NetworkMonitoringError):
        settings(**overrides)


def test_multiple_profiles_and_no_secret_repr():
    auths = parse_snmp_authentications({"snmp-yaml": AUTH_YAML})
    assert auths.names == ("cisco", "opnsense")
    assert yaml.safe_load(auths.yaml_text) == yaml.safe_load(AUTH_YAML)
    assert "test-auth-password" not in repr(auths)


def test_snmp_v2c_and_v3_profiles_can_share_a_secret_without_secret_repr():
    raw = AUTH_YAML + '  legacy:\n    version: 2\n    community: "test-v2c-community"\n'
    auths = parse_snmp_authentications({"snmp-yaml": raw})
    assert auths.names == ("cisco", "legacy", "opnsense")
    assert yaml.safe_load(auths.yaml_text) == yaml.safe_load(raw)
    assert "test-v2c-community" not in repr(auths)


@pytest.mark.parametrize("community", ["x", "12345", "private ! community"])
def test_snmp_v2c_requires_no_username_or_v3_passphrase_length(community):
    profile = {"version": 2, "community": community}
    auths = parse_snmp_authentications({"snmp-yaml": yaml.safe_dump({"auths": {"v2c": profile}})})
    assert yaml.safe_load(auths.yaml_text)["auths"]["v2c"] == profile


@pytest.mark.parametrize(
    "profile",
    [
        {"community": "test-secret-community"},
        {"version": 1, "community": "test-secret-community"},
        {"version": "2", "community": "test-secret-community"},
        {"version": "2c", "community": "test-secret-community"},
        {"version": 2.0, "community": "test-secret-community"},
        {"version": True, "community": "test-secret-community"},
        {"version": 2},
        {"version": 2, "community": ""},
        {"version": 2, "community": "   "},
        {"version": 2, "community": None},
        {"version": 2, "community": 12345},
        {"version": 2, "community": "test-secret-community\x00"},
        {"version": 2, "community": "test-secret-community", "username": "test-secret-user"},
        {"version": 2, "community": "test-secret-community", "security_level": "noAuthNoPriv"},
        {"version": 2, "community": "test-secret-community", "context_name": "test-context"},
        {"version": 3, "community": "test-secret-community"},
    ],
)
def test_invalid_v2c_or_mixed_version_profiles_are_rejected_without_secret_input(profile):
    with pytest.raises(NetworkMonitoringError) as error:
        parse_snmp_authentications({"snmp-yaml": yaml.safe_dump({"auths": {"device": profile}})})
    assert "test-secret" not in str(error.value)
    assert "12345" not in str(error.value)


def test_large_numeric_community_yaml_error_is_sanitized():
    raw = "auths:\n  legacy:\n    version: 2\n    community: " + "1" * 5000
    with pytest.raises(NetworkMonitoringError) as error:
        parse_snmp_authentications({"snmp-yaml": raw})
    assert "111111" not in str(error.value)


@pytest.mark.parametrize(
    "raw",
    [
        "auths: {}",
        "auths: {public: {version: 1, community: public}}",
        AUTH_YAML.replace("SHA256", "unsupported-secret-value"),
        AUTH_YAML.replace("test-auth-password", "short"),
        AUTH_YAML.replace("    version: 3", "    version: 3\n    version: 2", 1),
        AUTH_YAML + "modules: {}\n",
        "auths: [invalid]",
        "auths: {broken",
    ],
)
def test_invalid_profiles_do_not_echo_secret_input(raw):
    with pytest.raises(NetworkMonitoringError) as error:
        parse_snmp_authentications({"snmp-yaml": raw})
    assert "test-auth-password" not in str(error.value)
    assert "unsupported-secret-value" not in str(error.value)
    assert "broken" not in str(error.value)


@pytest.mark.parametrize("token", ["", "x\nAuthorization: injected", "x\x00y"])
def test_invalid_netbox_token(token):
    with pytest.raises(NetworkMonitoringError):
        parse_netbox_token({"token": token})


def test_network_requires_existing_sink_and_preserves_writer_identity():
    pipeline = NetworkPipeline(
        settings(), ("cisco", "opnsense"), "/private/token", "/private/snmp"
    )
    kwargs = {
        "loki_endpoints": [],
        "metrics_scrape_jobs": [],
        "systemd_units": [],
        "journal_kernel": False,
        "journal_match_expressions": [],
        "topology_labels": {},
        "network_pipeline": pipeline,
    }
    assert (
        'discovery.http "network_netbox"'
        not in ConfigBuilder(remote_write_endpoints=[], **kwargs).build()
    )
    rendered = ConfigBuilder(remote_write_endpoints=["http://mimir/api/v1/push"], **kwargs).build()
    assert 'prometheus.remote_write "metrics"' in rendered
    assert 'max_keepalive_time = "30m"' in rendered
    assert "credentials = local.file.network_netbox_token.content" in rendered
    assert 'config_merge_strategy = "merge"' in rendered
    assert 'replacement = "snmp/network"' in rendered
    assert 'replacement = "netbox-device-${1}"' in rendered
    assert "test-auth-password" not in rendered
    assert "region" not in rendered


def module_status_body(*targets):
    output = [
        {
            "type": "object",
            "value": [
                {"key": k, "value": {"type": "string", "value": v}} for k, v in labels.items()
            ],
        }
        for labels in targets
    ]
    return {"exports": [{"name": "output", "value": {"type": "array", "value": output}}]}


def test_module_status_reader_keeps_only_invalid_device_ids_and_sanitizes_them(monkeypatch):
    data = module_status_body(
        {"netbox_id": "1"},
        {"netbox_id": "42", "snmp_config_error": "invalid_modules"},
        {"netbox_id": "42", "snmp_config_error": "invalid_modules"},
        {"netbox_id": "private-value", "snmp_config_error": "invalid_modules"},
    )
    client = Mock()
    client.open.return_value = io.BytesIO(json.dumps(data).encode())
    monkeypatch.setattr(network_monitoring, "build_opener", lambda *_: client)
    assert read_snmp_module_errors("127.0.0.1:6987") == ("42", "unknown")
    client.open.assert_called_once_with(
        "http://127.0.0.1:6987/api/v0/web/components/discovery.relabel.network_scrape", timeout=2
    )


@pytest.mark.parametrize(
    "data",
    [None, [], {}, {"exports": {}}, {"exports": []}, {"exports": [{"name": "output"}]}],
)
def test_malformed_status_exports_are_unchecked_not_healthy(monkeypatch, data):
    client = Mock()
    client.open.return_value = io.BytesIO(json.dumps(data).encode())
    monkeypatch.setattr(network_monitoring, "build_opener", lambda *_: client)
    assert read_snmp_module_errors("127.0.0.1:6987") is None


def test_status_api_failure_and_oversized_body_are_unchecked(monkeypatch):
    client = Mock()
    monkeypatch.setattr(network_monitoring, "build_opener", lambda *_: client)
    client.open.side_effect = URLError("private-error-body")
    assert read_snmp_module_errors("127.0.0.1:6987") is None
    client.open.side_effect = None
    client.open.return_value = io.BytesIO(b"x" * (network_monitoring.MAX_MODULE_STATUS_BYTES + 1))
    assert read_snmp_module_errors("127.0.0.1:6987") is None


@pytest.mark.parametrize("data", [module_status_body(), module_status_body({"netbox_id": "1"})])
def test_valid_status_without_invalid_modules_is_healthy(monkeypatch, data):
    client = Mock()
    client.open.return_value = io.BytesIO(json.dumps(data).encode())
    monkeypatch.setattr(network_monitoring, "build_opener", lambda *_: client)
    assert read_snmp_module_errors("127.0.0.1:6987") == ()


@pytest.mark.parametrize(
    "target",
    [
        {"type": "string", "value": "bad-target"},
        {"type": "object", "value": [None]},
        {"type": "object", "value": [{"key": "snmp_config_error", "value": None}]},
        {
            "type": "object",
            "value": [
                {"key": "netbox_id", "value": {"type": "number", "value": "1"}},
            ],
        },
    ],
)
def test_malformed_target_status_is_unchecked(monkeypatch, target):
    data = {"exports": [{"name": "output", "value": {"type": "array", "value": [target]}}]}
    client = Mock()
    client.open.return_value = io.BytesIO(json.dumps(data).encode())
    monkeypatch.setattr(network_monitoring, "build_opener", lambda *_: client)
    assert read_snmp_module_errors("127.0.0.1:6987") is None
