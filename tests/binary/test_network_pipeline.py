"""Exercise the rendered rules in Alloy itself, using only loopback fixtures."""

import json
import os
import shutil
import socket
import subprocess
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, cast
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import urlopen

import pytest

import alloy
from config_builder import ConfigBuilder, LogSourceGroup, MetricsScrapeJob, ScrapeTarget
from network_monitoring import (
    NetworkPipeline,
    NetworkSettings,
    parse_snmp_authentications,
    read_snmp_module_errors,
    render_network_blocks,
)
from snmp_modules import INVALID_MODULE

BINARY = os.environ.get("ALLOY_BINARY", "")
pytestmark = pytest.mark.skipif(not BINARY, reason="Set ALLOY_BINARY to a tested Alloy executable")
TOPOLOGY = {
    "juju_model": "fixture-model",
    "juju_model_uuid": "00000000-0000-4000-8000-000000000001",
    "juju_application": "fixture-collector",
    "juju_unit": "fixture-collector/0",
    "juju_charm": "alloy-nm",
}


def device(device_id="123", **overrides):
    labels = {
        "__meta_netbox_id": device_id,
        "__meta_netbox_name": "router",
        "__meta_netbox_primary_ip4": "127.0.0.2",
        "__meta_netbox_site_slug": "home",
        "__meta_netbox_role": "Edge Router",
        "__meta_netbox_role_slug": "edge-router",
        "__meta_netbox_status": "active",
        "__meta_netbox_tag_slugs": "snmp,edge",
        "__meta_netbox_custom_field_snmp_auth": "opnsense",
        "__meta_netbox_contact_email": "private@example.invalid",
        "__param_target": "192.0.2.10",
        "juju_application": "wrong-owner",
        **overrides,
    }
    return {"targets": ["router.invalid"], "labels": labels}


@pytest.fixture
def server():
    state = {"inventory": [], "status": 200, "requests": [], "writes": [], "write_status": 204}

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, format: str, *args: object):
            pass

        def do_GET(self):  # noqa: N802 - HTTP server protocol
            state["requests"].append((self.path, self.headers.get("Authorization")))
            payload = json.dumps(state["inventory"]).encode()
            self.send_response(state["status"])
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def do_POST(self):  # noqa: N802 - HTTP server protocol
            state["writes"].append(self.rfile.read(int(self.headers["Content-Length"])))
            self.send_response(state["write_status"])
            self.end_headers()

    http = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=http.serve_forever, daemon=True)
    thread.start()
    state["url"] = f"http://127.0.0.1:{http.server_port}"
    try:
        yield state
    finally:
        http.shutdown()
        http.server_close()
        thread.join(timeout=5)


def wait_for(predicate, *, timeout=15):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            value = predicate()
            if value:
                return value
        except (URLError, ConnectionError):
            pass
        time.sleep(0.1)
    pytest.fail("Alloy fixture did not reach the expected state")


def decode(value: dict[str, Any]) -> Any:
    kind, data = value["type"], value["value"]
    if kind == "array":
        return [decode(item) for item in data]
    if kind == "object":
        return {item["key"]: decode(item["value"]) for item in data}
    return data


def targets(
    url, component="discovery.relabel.network_scrape", field="output"
) -> list[dict[str, str]]:
    with urlopen(f"{url}/api/v0/web/components/{component}", timeout=2) as response:
        component_data = json.load(response)
    return cast(
        list[dict[str, str]],
        decode(next(item["value"] for item in component_data["exports"] if item["name"] == field)),
    )


@pytest.fixture
def running_alloy(tmp_path, server):
    token, auth = tmp_path / "token", tmp_path / "snmp.yaml"
    token.write_text("fixture-token")
    auth.write_text(
        "auths:\n  opnsense:\n    version: 3\n    username: fixture\n"
        "    security_level: authPriv\n    auth_protocol: SHA256\n"
        "    password: fixture-auth-password\n    priv_protocol: AES\n"
        "    priv_password: fixture-privacy-password\n"
        "  cisco:\n    version: 3\n    username: switch\n"
        "    security_level: authNoPriv\n    auth_protocol: SHA\n"
        "    password: fixture-cisco-password\n"
        "  legacy_v2c:\n    version: 2\n    community: fixture-v2-community\n"
    )
    settings = NetworkSettings(
        discovery_url=server["url"] + "/devices/?site=home&tag=snmp&tag=edge",
        token_secret="",
        snmp_secret="",
        collector="lab",
        site="home",
        tags=("snmp", "edge"),
        refresh_interval="1s",
        scrape_interval="3s",
        scrape_timeout="1s",
    )
    profiles = parse_snmp_authentications({"snmp-yaml": auth.read_text()})
    pipeline = NetworkPipeline(settings, profiles.names, str(token), str(auth))
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        address = f"127.0.0.1:{listener.getsockname()[1]}"
    config = tmp_path / "config.alloy"
    config.write_text(
        'prometheus.remote_write "metrics" {\n endpoint {\n'
        f'  url = "{server["url"]}/push"\n'
        + " }\n}\n"
        + render_network_blocks(pipeline, topology_labels=TOPOLOGY)
        + '\nprometheus.scrape "fixture_self" {\n'
        + f'  targets = [{{"__address__" = "{address}", "job" = "alloy"}}]\n'
        + '  scrape_interval = "1s"\n'
        + '  scrape_timeout = "500ms"\n'
        + "  forward_to = [prometheus.remote_write.metrics.receiver]\n}\n"
    )
    result = subprocess.run([BINARY, "validate", str(config)], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    with (tmp_path / "alloy.log").open("w") as log:
        process = subprocess.Popen(
            [
                BINARY,
                "run",
                "--disable-reporting",
                f"--server.http.listen-addr={address}",
                f"--storage.path={tmp_path / 'data'}",
                str(config),
            ],
            stdout=log,
            stderr=log,
        )
        try:
            url = "http://" + address
            alloy.wait_ready(address, timeout=15)
            yield url, process, pipeline, config
        finally:
            process.terminate()
            try:
                process.wait(timeout=15)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)


def test_dynamic_inventory_identity_and_failure_semantics(server, running_alloy):
    url, process, _, _ = running_alloy
    pid = process.pid
    server["inventory"] = [
        device(),
        device("124", __meta_netbox_custom_field_snmp_auth="unknown"),
        device("125", __meta_netbox_primary_ip4="999.1.1.1"),
        device("126", __meta_netbox_primary_ip4=""),
        device("127", __meta_netbox_status="offline"),
        device("128", __meta_netbox_site_slug="another-site"),
        device("129", __meta_netbox_tag_slugs="snmp,not-edge"),
    ]
    selected = wait_for(lambda: targets(url))
    assert len(selected) == 1
    target = selected[0]
    public = {key: value for key, value in target.items() if not key.startswith("__")}
    assert public == {
        "job": "snmp/network",
        "instance": "127.0.0.2",
        "netbox_id": "123",
        "device": "router",
        "site": "home",
        "role": "edge-router",
        "collector": "lab",
        **TOPOLOGY,
    }
    assert target["__param_target"] == "127.0.0.2"
    assert target["__param_auth"] == "opnsense"
    assert target["__param_module"] == "system,if_mib"
    assert (
        target["__metrics_path__"] == "/api/v0/component/prometheus.exporter.snmp.network/metrics"
    )
    assert any(header == "Bearer fixture-token" for _, header in server["requests"])
    wait_for(lambda: server["writes"])

    server["inventory"] = [device(__meta_netbox_name="router-renamed")]
    renamed = wait_for(
        lambda: (result := targets(url)) and result[0]["device"] == "router-renamed" and result
    )
    assert renamed[0]["instance"] == "127.0.0.2"
    assert renamed[0]["netbox_id"] == "123"

    current = device(__meta_netbox_name="router-renamed", __meta_netbox_primary_ip4="127.0.0.3")
    server["inventory"] = [current]
    selected = wait_for(
        lambda: (result := targets(url)) and result[0]["instance"] == "127.0.0.3" and result
    )
    assert selected[0]["netbox_id"] == "123"
    assert selected[0]["__param_target"] == "127.0.0.3"

    current["labels"]["__meta_netbox_role_slug"] = "distribution-switch"
    selected = wait_for(
        lambda: (result := targets(url))
        and result[0].get("role") == "distribution-switch"
        and result
    )
    assert selected[0]["device"] == "router-renamed"
    for absent in (False, True):
        device_name = "router-role-missing" if absent else "router-role-empty"
        current["labels"]["__meta_netbox_name"] = device_name
        if absent:
            current["labels"].pop("__meta_netbox_role_slug")
            current["labels"].pop("__meta_netbox_role")
        else:
            current["labels"]["__meta_netbox_role_slug"] = ""
        selected = wait_for(
            lambda: (result := targets(url))
            and result[0]["device"] == device_name
            and "role" not in result[0]
            and result
        )
        assert selected[0]["netbox_id"] == "123"

    server["status"] = 500
    before = len(server["requests"])
    wait_for(lambda: len(server["requests"]) >= before + 2)
    assert targets(url) == selected
    server["status"] = 200
    server["inventory"] = []
    wait_for(lambda: not targets(url))
    assert process.pid == pid and process.poll() is None


def test_device_module_selection_changes_and_invalid_selection_is_observable(
    server, running_alloy
):
    url, process, _, _ = running_alloy
    server["inventory"] = [
        device(__meta_netbox_custom_field_snmp_modules="system,if_mib,cisco_device")
    ]
    selected = wait_for(lambda: targets(url))
    assert selected[0]["__param_module"] == "system,if_mib,cisco_device"
    assert "snmp_config_error" not in selected[0]
    pid = process.pid
    server["inventory"] = [device(__meta_netbox_custom_field_snmp_modules="not_loaded")]
    invalid = wait_for(lambda: next((t for t in targets(url) if t.get("snmp_config_error")), None))
    assert invalid["snmp_config_error"] == "invalid_modules"
    assert invalid["__param_module"] == INVALID_MODULE
    assert read_snmp_module_errors(url.removeprefix("http://")) == ("123",)
    query = f"?target=127.0.0.2&auth=opnsense&module={INVALID_MODULE}"
    with pytest.raises(HTTPError) as error:
        urlopen(url + invalid["__metrics_path__"] + query, timeout=2)
    assert b"Unknown module" in error.value.read()
    assert process.pid == pid and process.poll() is None
    for index, empty in enumerate(("", "None", "null"), start=400):
        server["inventory"] = [device(str(index), __meta_netbox_custom_field_snmp_modules=empty)]
        restored = wait_for(
            lambda: next(
                (
                    t
                    for t in targets(url)
                    if not t.get("snmp_config_error")
                    and t.get("netbox_id") == str(index)
                    and t.get("__param_module") == "system,if_mib"
                ),
                None,
            )
        )
        assert restored["__param_module"] == "system,if_mib"
        assert read_snmp_module_errors(url.removeprefix("http://")) == ()


def test_second_authentication_profile_and_credential_redaction(server, running_alloy):
    url, _, _, _ = running_alloy
    server["inventory"] = [device(__meta_netbox_custom_field_snmp_auth="cisco")]
    selected = wait_for(lambda: targets(url))
    assert selected[0]["__param_auth"] == "cisco"
    alloy.reload(api_address=url.removeprefix("http://"))
    assert targets(url) == selected
    with urlopen(
        url + "/api/v0/web/components/local.file.network_netbox_token", timeout=2
    ) as response:
        assert "fixture-token" not in response.read().decode()
    with urlopen(
        url + "/api/v0/web/components/local.file.network_snmp_auth", timeout=2
    ) as response:
        output = response.read().decode()
        assert "fixture-auth-password" not in output
        assert "fixture-privacy-password" not in output
        assert "fixture-v2-community" not in output


@pytest.fixture
def snmp_v2c_agent(tmp_path):
    binary = os.environ.get("SNMPD_BINARY") or shutil.which("snmpd")
    if not binary:
        pytest.skip("Install snmpd or set SNMPD_BINARY for actual SNMPv2c polling")
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as listener:
        listener.bind(("127.0.0.1", 0))
        address = f"127.0.0.1:{listener.getsockname()[1]}"
    config = tmp_path / "agent.conf"
    config.write_text(
        "rocommunity fixture-v2-community 127.0.0.1\n"
        "sysLocation alloy-v2c-fixture\nsysName alloy-v2c-fixture\n"
    )
    env = {
        **os.environ,
        "SNMPCONFPATH": str(tmp_path),
        "SNMP_PERSISTENT_DIR": str(tmp_path / "agent-state"),
        "MIBS": "",
    }
    with (tmp_path / "agent.log").open("w") as log:
        process = subprocess.Popen(
            [binary, "-f", "-Lo", "-C", "-c", str(config), "udp:" + address],
            env=env,
            stdout=log,
            stderr=log,
        )
        try:
            yield address, process
        finally:
            process.terminate()
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)


def test_v2c_profile_selects_and_polls_a_local_agent(server, running_alloy, snmp_v2c_agent):
    url, process, _, _ = running_alloy
    address, agent = snmp_v2c_agent
    server["inventory"] = [device(__meta_netbox_custom_field_snmp_auth="legacy_v2c")]
    selected = wait_for(lambda: targets(url))
    target = selected[0]
    assert target["__param_auth"] == "legacy_v2c"
    assert target["instance"] == "127.0.0.2"
    assert target["juju_application"] == TOPOLOGY["juju_application"]
    parameters = urlencode({"target": address, "auth": target["__param_auth"], "module": "system"})

    def collected():
        assert agent.poll() is None, "Isolated SNMP agent exited unexpectedly"
        with urlopen(url + target["__metrics_path__"] + "?" + parameters, timeout=10) as response:
            return response.read().decode()

    metrics = wait_for(collected)
    assert "sysUpTime " in metrics
    assert 'sysLocation="alloy-v2c-fixture"' in metrics
    assert "fixture-v2-community" not in metrics
    with urlopen(url + "/api/v0/web/components/local.file.network_snmp_auth", timeout=2) as r:
        assert "fixture-v2-community" not in r.read().decode()
    assert process.poll() is None


def test_malformed_config_reload_retains_previous_pipeline(server, running_alloy):
    url, process, _, config = running_alloy
    server["inventory"] = [device()]
    selected = wait_for(lambda: targets(url))
    config.write_text('prometheus.scrape "invalid" {\n targets = discovery.missing.targets\n}\n')
    with pytest.raises(URLError):
        alloy.reload(api_address=url.removeprefix("http://"))
    assert targets(url) == selected
    assert process.poll() is None


def test_complete_collection_config_validates_with_collector_and_provider_topology(
    server, running_alloy, tmp_path
):
    _, _, pipeline, _ = running_alloy
    provider = {**TOPOLOGY, "juju_application": "source-app", "juju_unit": "source-app/0"}
    rendered = ConfigBuilder(
        loki_endpoints=[server["url"] + "/loki/api/v1/push"],
        remote_write_endpoints=[server["url"] + "/push"],
        metrics_scrape_jobs=[
            MetricsScrapeJob("provider", [ScrapeTarget("127.0.0.1:9999", provider)])
        ],
        systemd_units=["alloy.service"],
        journal_kernel=True,
        journal_match_expressions=["SYSLOG_IDENTIFIER=fixture"],
        enable_syslog_receivers=True,
        topology_labels=TOPOLOGY,
        log_source_groups=[
            LogSourceGroup("source_app", provider, systemd_units=["fixture.service"])
        ],
        network_pipeline=pipeline,
        log_level="warning",
    ).build()
    config = tmp_path / "all-collection-paths.alloy"
    config.write_text(rendered)
    result = subprocess.run([BINARY, "validate", str(config)], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    assert 'level = "warn"' in rendered
    provider_scrape = rendered.split('prometheus.scrape "provider" {', 1)[1].split("\n}\n", 1)[0]
    assert 'juju_application = "source-app"' in provider_scrape
    assert 'juju_application = "fixture-collector"' not in provider_scrape


def test_host_journal_without_service_sources_has_resolvable_relabel_rules(server, tmp_path):
    rendered = ConfigBuilder(
        loki_endpoints=[server["url"] + "/loki/api/v1/push"],
        remote_write_endpoints=[],
        metrics_scrape_jobs=[],
        systemd_units=[],
        journal_kernel=True,
        journal_match_expressions=["SYSLOG_IDENTIFIER=fixture"],
        topology_labels=TOPOLOGY,
    ).build()
    config = tmp_path / "host-only.alloy"
    config.write_text(rendered)
    result = subprocess.run([BINARY, "validate", str(config)], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr


def test_wal_replays_after_restart_with_collector_self_scraping(server, running_alloy):
    url, process, _, config = running_alloy
    server["write_status"] = 503
    server["inventory"] = [device()]
    wait_for(lambda: targets(url))
    wal = config.parent / "data" / "prometheus.remote_write.metrics" / "wal"
    wait_for(lambda: any(b"snmp/network" in path.read_bytes() for path in wal.iterdir()))
    wait_for(lambda: server["writes"])
    process.terminate()
    try:
        process.wait(timeout=15)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=5)
    count = len(server["writes"])
    server["inventory"] = []
    server["write_status"] = 204
    with (config.parent / "restart.log").open("w") as log:
        restarted = subprocess.Popen(process.args, stdout=log, stderr=log)
        try:
            wait_for(lambda: urlopen(url + "/-/ready", timeout=2).status == 200)
            assert not targets(url)
            wait_for(lambda: len(server["writes"]) > count)
            assert restarted.pid != process.pid
            wait_for(lambda: "Done replaying WAL" in (config.parent / "restart.log").read_text())
        finally:
            restarted.terminate()
            try:
                restarted.wait(timeout=15)
            except subprocess.TimeoutExpired:
                restarted.kill()
                restarted.wait(timeout=5)
