"""Exercise generated RFC3164 listeners and the existing RFC5424 listener in Alloy."""

import os
import socket
import subprocess
import time
from pathlib import Path

import pytest

import alloy
from config_builder import ConfigBuilder

BINARY = os.environ.get("ALLOY_BINARY", "")
pytestmark = pytest.mark.skipif(not BINARY, reason="Set ALLOY_BINARY for native syslog tests")


def _free_port():
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        return listener.getsockname()[1]


@pytest.mark.parametrize("rfc5424", [False, True])
def test_native_separate_syslog_receivers_keep_identity_and_parse_cisco(tmp_path, rfc5424):
    legacy_port, modern_port, http_port = (_free_port() for _ in range(3))
    builder = ConfigBuilder(
        loki_endpoints=["http://127.0.0.1:9/loki/api/v1/push"],
        remote_write_endpoints=[],
        metrics_scrape_jobs=[],
        systemd_units=[],
        journal_kernel=False,
        journal_match_expressions=[],
        enable_syslog_rfc3164=True,
        syslog_rfc3164_port=legacy_port,
        enable_syslog_receivers=rfc5424,
        syslog_parsers=("cisco_ios",),
        topology_labels={"juju_model": "native", "juju_application": "alloy-test"},
    )
    # Echo the actual generated processor output, including extracted parser values.
    probe = (
        '  stage.template {\n    source = "probe"\n'
        '    template = "{{ .Entry }}##vendor={{ .event_vendor }}##type={{ .event_type }}"\n'
        '  }\n  stage.output { source = "probe" }\n'
        "  forward_to = [loki.echo.output.receiver]"
    )
    text = builder.build().replace(builder._render_loki_writer(), 'loki.echo "output" {}')
    text = text.replace("  forward_to = [loki.write.main.receiver]", probe)
    text = text.replace(":1514", f":{modern_port}")
    config = tmp_path / "syslog.alloy"
    config.write_text(text)
    binary = str(Path(BINARY).resolve())
    validated = subprocess.run([binary, "validate", str(config)], capture_output=True, text=True)
    assert validated.returncode == 0, validated.stderr
    output = tmp_path / "alloy.log"
    with output.open("w") as log:
        process = subprocess.Popen(
            [
                binary,
                "run",
                "--disable-reporting",
                f"--server.http.listen-addr=127.0.0.1:{http_port}",
                f"--storage.path={tmp_path / 'data'}",
                str(config),
            ],
            stdout=log,
            stderr=log,
        )
        try:
            alloy.wait_ready(f"127.0.0.1:{http_port}", timeout=15)
            expected = []
            for format_name, port in [
                ("rfc3164", legacy_port),
                *([("rfc5424", modern_port)] if rfc5424 else []),
            ]:
                for protocol in ("tcp", "udp"):
                    body = (
                        "%LINK-3-UPDOWN: Interface GigabitEthernet1/1, changed state to down "
                        f"{format_name}-{protocol}"
                    )
                    header = (
                        "<27>Oct 11 22:14:15 legacy-switch ios: "
                        if format_name == "rfc3164"
                        else "<27>1 2026-10-20T10:00:00Z modern-switch ios 123 - - "
                    )
                    payload = (header + body + "\n").encode()
                    if protocol == "tcp":
                        with socket.create_connection(("127.0.0.1", port), timeout=5) as sender:
                            sender.sendall(payload)
                    else:
                        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sender:
                            sender.sendto(payload, ("127.0.0.1", port))
                    expected.append(body + "##vendor=cisco##type=interface_state")
            deadline = time.monotonic() + 10
            while time.monotonic() < deadline:
                if all(line in output.read_text() for line in expected):
                    break
                time.sleep(0.1)
            logs = output.read_text()
            assert all(line in logs for line in expected), logs
            assert "legacy-switch" in logs
            assert "alloy-test" in logs
        finally:
            process.terminate()
            process.wait(timeout=15)
