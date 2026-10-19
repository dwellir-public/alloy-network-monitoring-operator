"""Check actual Alloy parser execution using loopback log ingress and echo output."""

import json
import os
import socket
import subprocess
import time
from urllib.request import Request, urlopen

import pytest

import alloy
from network_syslog import render_syslog_parser_stages

BINARY = os.environ.get("ALLOY_BINARY", "")
pytestmark = pytest.mark.skipif(not BINARY, reason="Set ALLOY_BINARY for native parser tests")


def free_port():
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        return listener.getsockname()[1]


def test_native_opnsense_and_cisco_parsers_keep_original_lines_and_extract_expected_fields(
    tmp_path,
):
    api_port, alloy_port = free_port(), free_port()
    fields = (
        "event_vendor",
        "event_type",
        "event_ip_version",
        "event_protocol",
        "event_src_port",
        "event_dst_port",
        "event_interface",
        "event_state",
    )
    template = "{{ .Entry }}" + "".join("##" + field + "={{ ." + field + " }}" for field in fields)
    stages = "\n".join(render_syslog_parser_stages(("opnsense_filterlog", "cisco_ios")))
    config = tmp_path / "parser.alloy"
    config.write_text(
        'logging { level = "info" }\n'
        'loki.echo "output" {}\n'
        'loki.process "parser" {\n' + stages + "\n"
        'stage.template { source = "test_probe"\n template = ' + json.dumps(template) + " }\n"
        'stage.output { source = "test_probe" }\n'
        "forward_to = [loki.echo.output.receiver]\n}\n"
        'loki.source.api "input" {\n http {\n listen_address = "127.0.0.1"\n'
        f"listen_port = {api_port}\n}}\n forward_to = [loki.process.parser.receiver]\n}}\n"
    )
    result = subprocess.run([BINARY, "validate", str(config)], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    logs = tmp_path / "alloy.log"
    with logs.open("w") as log:
        process = subprocess.Popen(
            [
                BINARY,
                "run",
                "--disable-reporting",
                f"--server.http.listen-addr=127.0.0.1:{alloy_port}",
                f"--storage.path={tmp_path / 'data'}",
                str(config),
            ],
            stdout=log,
            stderr=log,
        )
        try:
            alloy.wait_ready(f"127.0.0.1:{alloy_port}", timeout=15)
            cases = [
                (
                    "filterlog",
                    "100,,,123,igb0,match,block,in,4,0x0,,64,0,0,none,6,tcp,60,192.0.2.1,198.51.100.1,12345,443,0,S,,,65535,,",
                    "opnsense",
                    "firewall",
                    "4",
                    "tcp",
                    "12345",
                    "443",
                    "igb0",
                    "<no value>",
                ),
                (
                    "filterlog",
                    "101,,,124,igb1,match,pass,out,6,0x0,0,64,udp,17,80,2001:db8::1,2001:db8::2,5353,53,32",
                    "opnsense",
                    "firewall",
                    "6",
                    "udp",
                    "5353",
                    "53",
                    "igb1",
                    "<no value>",
                ),
                (
                    "filterlog",
                    "102,,,125,igb0,match,block,in,4,0x0,,64,0,0,none,1,icmp,84,192.0.2.1,198.51.100.1,request,1,2",
                    "opnsense",
                    "firewall",
                    "4",
                    "icmp",
                    "<no value>",
                    "<no value>",
                    "igb0",
                    "<no value>",
                ),
                (
                    "ios",
                    "%LINK-3-UPDOWN: Interface TenGigabitEthernet1/1, changed state to down",
                    "cisco",
                    "interface_state",
                    "<no value>",
                    "<no value>",
                    "<no value>",
                    "<no value>",
                    "TenGigabitEthernet1/1",
                    "down",
                ),
                (
                    "ios",
                    "%SEC_LOGIN-4-LOGIN_FAILED: Login failed for user monitor",
                    "cisco",
                    "authentication_failure",
                    "<no value>",
                    "<no value>",
                    "<no value>",
                    "<no value>",
                    "<no value>",
                    "<no value>",
                ),
                (
                    "ios",
                    "%LINEPROTO-5-UPDOWN: Line protocol on Interface TenGigabitEthernet1/2, "
                    "changed state to up",
                    "cisco",
                    "interface_state",
                    "<no value>",
                    "<no value>",
                    "<no value>",
                    "<no value>",
                    "TenGigabitEthernet1/2",
                    "up",
                ),
                (
                    "ios",
                    "%ENVMON-3-FAN_FAIL: Fan reports a fault",
                    "cisco",
                    "hardware_alarm",
                    "<no value>",
                    "<no value>",
                    "<no value>",
                    "<no value>",
                    "<no value>",
                    "<no value>",
                ),
                (
                    "ios",
                    "%SYS-5-CONFIG_I: Configured from console by monitor",
                    "cisco",
                    "configuration_change",
                    "<no value>",
                    "<no value>",
                    "<no value>",
                    "<no value>",
                    "<no value>",
                    "<no value>",
                ),
                ("filterlog", "truncated,unknown,message", *("<no value>",) * 8),
            ]
            streams = [
                {
                    "stream": {"log_source": "remote_syslog", "syslog_app_name": case[0]},
                    "values": [[str(time.time_ns()), case[1]]],
                }
                for case in cases
            ]
            request = Request(
                f"http://127.0.0.1:{api_port}/loki/api/v1/push",
                data=json.dumps({"streams": streams}).encode(),
                headers={"Content-Type": "application/json"},
            )
            with urlopen(request, timeout=5) as response:
                assert response.status == 204
            expected = [
                case[1]
                + "".join("##" + field + "=" + value for field, value in zip(fields, case[2:]))
                for case in cases
            ]
            deadline = time.monotonic() + 10
            while time.monotonic() < deadline and not all(
                item in logs.read_text() for item in expected
            ):
                time.sleep(0.1)
            output = logs.read_text()
            assert all(item in output for item in expected), output
        finally:
            process.terminate()
            process.wait(timeout=15)
