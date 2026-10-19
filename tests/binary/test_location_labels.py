"""Verify location defaults after real Alloy metric and log HTTP forwarding."""

import json
import os
import re
import socket
import subprocess
import time
from datetime import datetime, timezone
from urllib.request import Request, urlopen

import pytest

import alloy
from config_builder import ConfigBuilder, MetricsScrapeJob, ScrapeTarget

BINARY = os.environ.get("ALLOY_BINARY", "")
pytestmark = pytest.mark.skipif(not BINARY, reason="Set ALLOY_BINARY for native forwarding tests")
TOPOLOGY = {
    "juju_model": "fixture-model",
    "juju_model_uuid": "00000000-0000-4000-8000-000000000001",
    "juju_application": "fixture-collector",
    "juju_unit": "fixture-collector/0",
    "juju_charm": "alloy-network-monitoring",
}
SOURCE = {"site": "source-site", "region": "source-region", "juju_application": "source-app"}


def free_ports(count):
    ports = set()
    while len(ports) < count:
        with socket.socket() as listener:
            listener.bind(("127.0.0.1", 0))
            ports.add(listener.getsockname()[1])
    return tuple(ports)


def metric_probe(name, expected):
    expression = ";".join(["go_info", name, *(expected.get(k, "") for k in SOURCE)])
    return (
        f'prometheus.relabel "{name}" {{\n'
        '  rule {\n    action = "keep"\n'
        '    source_labels = ["__name__", "job", "site", "region", "juju_application"]\n'
        f"    regex = {json.dumps(expression)}\n  }}\n"
        "  forward_to = []\n}\n"
    )


@pytest.mark.parametrize(
    "location",
    [
        {},
        {"site": "scope-site"},
        {"region": "scope-region"},
        {"site": "scope-site", "region": "scope-region"},
    ],
)
def test_forwarded_locations_preserve_source_labels_and_syslog_budget(tmp_path, location):
    api, metrics, logs, ingress, syslog = free_ports(5)
    jobs = [
        MetricsScrapeJob(
            name,
            [ScrapeTarget(f"127.0.0.1:{api}", labels)],
            scrape_interval="1s",
            scrape_timeout="500ms",
        )
        for name, labels in (("location_default", {}), ("location_explicit", SOURCE))
    ]
    config = ConfigBuilder(
        loki_endpoints=[f"http://127.0.0.1:{logs}/loki/api/v1/push"],
        remote_write_endpoints=[f"http://127.0.0.1:{metrics}/api/v1/metrics/write"],
        metrics_scrape_jobs=jobs,
        systemd_units=[],
        journal_kernel=False,
        journal_match_expressions=[],
        enable_syslog_receivers=True,
        topology_labels=TOPOLOGY,
        self_metrics_address=f"127.0.0.1:{api}",
        location_labels=location,
    ).build()
    config = config.replace("logging {\n", 'logging {\n  format = "json"\n', 1)
    config = config.replace('":1514"', f'"127.0.0.1:{syslog}"')
    config += metric_probe("location_default", {**location, **TOPOLOGY})
    config += metric_probe("location_explicit", SOURCE)
    config += (
        'prometheus.receive_http "backend" {\n'
        f'  http {{\n    listen_address = "127.0.0.1"\n    listen_port = {metrics}\n  }}\n'
        "  forward_to = [prometheus.relabel.location_default.receiver, "
        "prometheus.relabel.location_explicit.receiver]\n}\n"
        'loki.echo "backend" {}\n'
        'loki.source.api "backend" {\n'
        f'  http {{\n    listen_address = "127.0.0.1"\n    listen_port = {logs}\n  }}\n'
        "  forward_to = [loki.echo.backend.receiver]\n}\n"
        'loki.source.api "producer" {\n'
        f'  http {{\n    listen_address = "127.0.0.1"\n    listen_port = {ingress}\n  }}\n'
        "  forward_to = [loki.write.main.receiver]\n}\n"
    )
    path = tmp_path / "locations.alloy"
    path.write_text(config)
    validation = subprocess.run([BINARY, "validate", str(path)], capture_output=True, text=True)
    assert validation.returncode == 0, validation.stderr
    output = tmp_path / "alloy.log"
    with output.open("w") as stream:
        process = subprocess.Popen(
            [
                BINARY,
                "run",
                "--disable-reporting",
                f"--server.http.listen-addr=127.0.0.1:{api}",
                f"--storage.path={tmp_path / 'data'}",
                str(path),
            ],
            stdout=stream,
            stderr=stream,
        )
        try:
            alloy.wait_ready(f"127.0.0.1:{api}", timeout=15)
            stamp = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
            message = "location-default-event"
            payload = f"<134>1 {stamp} fixture-device ios 123 event - {message}"
            with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sender:
                sender.sendto(payload.encode(), ("127.0.0.1", syslog))
            request = Request(
                f"http://127.0.0.1:{ingress}/loki/api/v1/push",
                data=json.dumps(
                    {
                        "streams": [
                            {
                                "stream": SOURCE,
                                "values": [[str(time.time_ns()), "location-explicit-event"]],
                            }
                        ]
                    }
                ).encode(),
                headers={"Content-Type": "application/json"},
            )
            with urlopen(request, timeout=5) as response:
                assert response.status == 204
            received = {}
            deadline = time.monotonic() + 25
            while time.monotonic() < deadline:
                for line in output.read_text().splitlines():
                    entry = json.loads(line)
                    if entry.get("component_id") == "loki.echo.backend":
                        received[entry["entry"]] = entry
                with urlopen(f"http://127.0.0.1:{api}/metrics", timeout=5) as response:
                    counters = response.read().decode()
                passed = re.findall(
                    r'prometheus_relabel_metrics_written\{[^\n]*component_id="prometheus.relabel.'
                    r'(location_default|location_explicit)"[^\n]*\} ([0-9.e+]+)',
                    counters,
                )
                if {name for name, value in passed if float(value) > 0} == {
                    "location_default",
                    "location_explicit",
                } and len(received) == 2:
                    break
                time.sleep(0.1)
            else:
                pytest.fail("Forwarded location labels did not match:\n" + output.read_text())
            for message, expected in (
                ("location-default-event", {**location, **TOPOLOGY}),
                ("location-explicit-event", SOURCE),
            ):
                labels = dict(re.findall(r'(\w+)="([^"\\]*)"', received[message]["labels"]))
                assert len(labels) <= 15
                for key in ("site", "region", *TOPOLOGY):
                    assert labels.get(key) == expected.get(key)
            if len(location) == 2:
                assert '"protocol"' in received["location-default-event"]["structured_metadata"]
                assert '"udp"' in received["location-default-event"]["structured_metadata"]
        finally:
            process.terminate()
            try:
                process.wait(timeout=15)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)
