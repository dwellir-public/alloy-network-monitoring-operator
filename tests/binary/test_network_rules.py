"""Evaluate generated PromQL against counter/gauge scenarios with optional promtool."""

import json
import os
import subprocess
from typing import Any, cast

import pytest
import yaml

from network_alert_settings import parse_alert_settings
from network_alerts import device_alert_groups

PROMTOOL = os.environ.get("PROMTOOL_BINARY", "")
pytestmark = pytest.mark.skipif(not PROMTOOL, reason="Set PROMTOOL_BINARY for rule evaluation")
TOPOLOGY = {"juju_model_uuid": "fixture-model", "juju_application": "collector"}
LABELS = {
    **TOPOLOGY,
    "job": "snmp/network",
    "collector": "lab",
    "instance": "192.0.2.1",
    "netbox_id": "1",
    "device": "router",
}
INTERFACE = {"ifIndex": "1", "ifName": "wan"}


def series(metric, values, **labels):
    selectors = ",".join(f"{k}={json.dumps(v)}" for k, v in {**LABELS, **labels}.items())
    return {"series": metric + "{" + selectors + "}", "values": values}


def rules():
    settings = parse_alert_settings(
        {
            "network-device-alerts-enabled": True,
            "network-alerts-config": "interfaces: [{name: wan, include: wan}]",
        }
    )
    groups = device_alert_groups("lab", "fixture", topology_labels=TOPOLOGY, settings=settings)
    return groups, {
        r["alert"]: r for group in groups for r in cast(list[dict[str, Any]], group["rules"])
    }


def test_promql_semantics_pending_and_recovery(tmp_path):
    groups, alerts = rules()
    cases = []

    def check(name, inputs, count, *, eval_time="10m"):
        cases.append(
            {
                "interval": "1m",
                "input_series": inputs,
                "promql_expr_test": [
                    {
                        "expr": "count(" + alerts[name]["expr"] + ") or vector(0)",
                        "eval_time": eval_time,
                        "exp_samples": [{"labels": "{}", "value": count}],
                    }
                ],
            }
        )

    down = series("ifOperStatus", "2x20", **INTERFACE)
    check("NetworkInterfaceDown", [down, series("ifAdminStatus", "1x20", **INTERFACE)], 1)
    check("NetworkInterfaceDown", [down, series("ifAdminStatus", "2x20", **INTERFACE)], 0)
    check(
        "NetworkInterfaceDown",
        [series("ifOperStatus", "2x20", ifName="unused", ifIndex="1")],
        0,
    )
    check(
        "NetworkInterfaceDown",
        [
            series("ifOperStatus", "2x20", **INTERFACE, juju_application="other"),
            series("ifAdminStatus", "1x20", **INTERFACE, juju_application="other"),
        ],
        0,
    )
    octets = series("ifHCInOctets", "0+6750000000x20", **INTERFACE)
    check(
        "NetworkInterfaceInUtilisationHigh",
        [octets, series("ifHighSpeed", "1000x20", **INTERFACE)],
        1,
    )
    check(
        "NetworkInterfaceOutUtilisationHigh",
        [
            octets,
            series("ifHCOutOctets", "0+60000000x20", **INTERFACE),
            series("ifHighSpeed", "1000x20", **INTERFACE),
        ],
        0,
    )
    check(
        "NetworkInterfaceInUtilisationHigh",
        [octets, series("ifHighSpeed", "0x20", **INTERFACE)],
        0,
    )
    for packet_increment, error_increment, expected in [(6000, 120, 1), (6, 2, 0)]:
        inputs = [
            series("ifHCInUcastPkts", f"0+{packet_increment}x20", **INTERFACE),
            series("ifHCInMulticastPkts", "0x20", **INTERFACE),
            series("ifHCInBroadcastPkts", "0x20", **INTERFACE),
            series("ifInErrors", f"0+{error_increment}x20", **INTERFACE),
        ]
        check("NetworkInterfaceInErrorsHigh", inputs, expected)
    check("NetworkCPUHigh", [series("cpmCPUTotal1minRev", "95x20")], 1)
    check("NetworkCPUHigh", [], 0)
    for state, expected in [("failed", 1), ("offAdmin", 0), ("on", 0)]:
        check(
            "NetworkPowerFault",
            [series("cefcFRUPowerOperStatus", "1x20", cefcFRUPowerOperStatus=state)],
            expected,
        )
    for status, expected in [(3, 1), (2, 0)]:
        check("NetworkSensorFault", [series("entSensorStatus", f"{status}x20")], expected)
    for values, expected in [
        ("100000+6000x5 0+6000x14", 1),
        ("4147200000+6000x5 0+6000x14", 1),
        ("4294900000 1000+6000x19", 0),
    ]:
        check("NetworkSNMPUptimeReset", [series("sysUpTime", values)], expected)
    for enhanced, expected in [(True, 0), (False, 1)]:
        inputs = [series("ciscoMemoryPoolUsed", "95x20"), series("ciscoMemoryPoolFree", "5x20")]
        if enhanced:
            inputs += [series("cempMemPoolUsed", "50x20"), series("cempMemPoolFree", "50x20")]
        check("NetworkMemoryHigh", inputs, expected)

    down_alert = alerts["NetworkInterfaceDown"]
    cases.append(
        {
            "interval": "1m",
            "input_series": [
                series("ifOperStatus", "1x4 2x5 1x10", **INTERFACE),
                series("ifAdminStatus", "1x20", **INTERFACE),
            ],
            "alert_rule_test": [
                {"eval_time": "9m", "alertname": down_alert["alert"], "exp_alerts": []},
                {
                    "eval_time": "10m",
                    "alertname": down_alert["alert"],
                    "exp_alerts": [
                        {
                            "exp_labels": {**LABELS, **INTERFACE, **down_alert["labels"]},
                            "exp_annotations": down_alert["annotations"],
                        }
                    ],
                },
                {"eval_time": "11m", "alertname": down_alert["alert"], "exp_alerts": []},
            ],
        }
    )
    rule_path = tmp_path / "network.rules.yaml"
    rule_path.write_text(yaml.safe_dump({"groups": groups}))
    test_path = tmp_path / "network.test.yaml"
    test_path.write_text(
        yaml.safe_dump(
            {"rule_files": [str(rule_path)], "evaluation_interval": "1m", "tests": cases}
        )
    )
    result = subprocess.run(
        [PROMTOOL, "test", "rules", str(test_path)], capture_output=True, text=True, timeout=30
    )
    assert result.returncode == 0, result.stdout + result.stderr
