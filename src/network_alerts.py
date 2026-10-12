"""Collector-scoped Prometheus rules carried by the existing rule transport."""

import json
from collections.abc import Mapping


def network_alert_groups(
    collector: str, owner: str, *, topology_labels: Mapping[str, str] | None = None
) -> list[dict[str, object]]:
    """Build collector-scoped rules with the collector's Juju provenance."""
    topology = topology_labels or {}
    scope = ",".join(
        [f"collector={json.dumps(collector)}"]
        + [
            f"{key}={json.dumps(topology[key])}"
            for key in ("juju_model_uuid", "juju_application")
            if topology.get(key)
        ]
    )
    device = f'job="snmp/network",{scope}'
    local = f'job="alloy",{scope}'
    rules = [
        ("SNMPDeviceDown", f"up{{{device}}} == 0", "5m", "SNMP collection is failing"),
        ("SNMPNoEligibleTargets", f"absent(up{{{device}}})", "5m", "No eligible SNMP targets"),
        (
            "NetBoxDiscoveryFailing",
            f"increase(prometheus_sd_http_failures_total{{{scope}}}[5m]) > 0",
            "2m",
            "NetBox discovery refresh is failing",
        ),
        (
            "NetworkCollectorDown",
            f"absent(up{{{local}}}) or up{{{local}}} == 0",
            "5m",
            "Network collector self metrics are missing",
        ),
        (
            "NetworkRemoteWriteBacklog",
            f"prometheus_remote_storage_samples_pending{{{scope}}} > 10000",
            "10m",
            "Network collector remote write is falling behind",
        ),
        (
            "NetworkCollectorDiskLow",
            f'node_filesystem_avail_bytes{{{scope},fstype!="tmpfs"}} / '
            f'node_filesystem_size_bytes{{{scope},fstype!="tmpfs"}} < 0.10',
            "10m",
            "Network collector filesystem has less than 10% free space",
        ),
    ]
    return [
        {
            "name": f"alloy-network-{owner}",
            "rules": [
                {
                    "alert": name,
                    "expr": expression,
                    "for": duration,
                    "labels": {
                        **(topology_labels or {}),
                        "severity": "warning",
                        "collector": collector,
                    },
                    "annotations": {"summary": summary},
                }
                for name, expression, duration, summary in rules
            ],
        }
    ]
