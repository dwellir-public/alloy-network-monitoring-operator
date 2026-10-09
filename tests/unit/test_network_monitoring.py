from urllib.parse import parse_qs, urlsplit

import pytest
import yaml

from config_builder import ConfigBuilder
from network_monitoring import (
    NetworkMonitoringError,
    NetworkPipeline,
    parse_netbox_token,
    parse_network_settings,
    parse_snmp_authentications,
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


@pytest.mark.parametrize(
    "raw",
    [
        "auths: {}",
        "auths: {public: {version: 2, community: public}}",
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
