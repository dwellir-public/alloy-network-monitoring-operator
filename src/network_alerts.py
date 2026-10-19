"""Collector-scoped Prometheus rules carried by the existing rule transport."""

import json
from collections.abc import Mapping

from network_alert_settings import NetworkAlertSettings


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


def _scope(collector: str, topology: Mapping[str, str]) -> str:
    return ",".join(
        [f"collector={json.dumps(collector)}"]
        + [
            f"{key}={json.dumps(topology[key])}"
            for key in ("juju_model_uuid", "juju_application")
            if topology.get(key)
        ]
    )


def _alert(name, expr, preset, summary, labels):
    return {
        "alert": name,
        "expr": expr,
        "for": preset["for"],
        "labels": {**labels, "severity": "warning"},
        "annotations": {"summary": summary},
    }


INTERFACE_KEYS = "juju_model_uuid,juju_application,collector,instance,netbox_id,ifIndex"
DEVICE_KEYS = "juju_model_uuid,juju_application,collector,instance,netbox_id"
TIMETICKS_WRAP_GUARD = 2**32 - 15 * 60 * 100


def _interface_rules(scope: str, presets: dict, labels: dict) -> list[dict]:
    on = f"on({INTERFACE_KEYS})"
    rules = []
    if presets["interface_down"]["enabled"]:
        rules.append(
            _alert(
                "NetworkInterfaceDown",
                f"ifOperStatus{{{scope}}} == 2 and {on} ifAdminStatus{{{scope}}} == 1",
                presets["interface_down"],
                "An expected active network interface is down",
                labels,
            )
        )
    for direction in ("In", "Out"):
        if presets["utilisation"]["enabled"]:
            expr = (
                f"(rate(ifHC{direction}Octets{{{scope}}}[5m]) * 8 / {on} "
                f"(ifHighSpeed{{{scope}}} * 1000000) * 100 > "
                f"{presets['utilisation']['threshold']}) "
                f"and {on} ifHighSpeed{{{scope}}} > 0"
            )
            rules.append(
                _alert(
                    f"NetworkInterface{direction}UtilisationHigh",
                    expr,
                    presets["utilisation"],
                    "Network interface utilisation is sustained above its threshold",
                    labels,
                )
            )
        packets = (
            "("
            + f" + {on} ".join(
                f"rate(ifHC{direction}{kind}Pkts{{{scope}}}[5m])"
                for kind in ("Ucast", "Multicast", "Broadcast")
            )
            + ")"
        )
        for preset, metric in (("errors", "Errors"), ("discards", "Discards")):
            option = presets[preset]
            if option["enabled"]:
                expr = (
                    f"(rate(if{direction}{metric}{{{scope}}}[5m]) / {on} "
                    f"clamp_min({packets}, 1) * 100 > {option['threshold']}) "
                    f"and {on} {packets} >= {option['min_packets']}"
                )
                rules.append(
                    _alert(
                        f"NetworkInterface{direction}{metric}High",
                        expr,
                        option,
                        "Network interface error/discard ratio is sustained above its threshold",
                        labels,
                    )
                )
    return rules


def device_alert_groups(
    collector: str,
    owner: str,
    *,
    topology_labels: Mapping[str, str],
    settings: NetworkAlertSettings,
) -> list[dict[str, object]]:
    """Build opt-in metric rules; absent capabilities do not fabricate failures."""
    if not settings.metrics_enabled or not settings.devices_enabled:
        return []
    labels = {**topology_labels, "collector": collector}
    scope = 'job="snmp/network",' + _scope(collector, topology_labels)
    rules = [
        _alert(
            "SNMPModuleSelectionInvalid",
            f'up{{{scope},snmp_config_error="invalid_modules"}} == 0',
            {"for": "2m"},
            "NetBox selects an unknown or malformed SNMP module list",
            labels,
        )
    ]
    presets = settings.presets
    if presets["cpu"]["enabled"]:
        rules.append(
            _alert(
                "NetworkCPUHigh",
                f"cpmCPUTotal1minRev{{{scope}}} > {presets['cpu']['threshold']}",
                presets["cpu"],
                "Network device CPU utilisation is sustained above its threshold",
                labels,
            )
        )
    if presets["memory"]["enabled"]:
        rules.append(
            _alert(
                "NetworkMemoryHigh",
                _memory_expression(scope, presets["memory"]["threshold"]),
                presets["memory"],
                "Network device memory pool usage is sustained above its threshold",
                labels,
            )
        )
    if presets["power"]["enabled"]:
        faults = (
            "offEnvPower|offEnvTemp|offEnvFan|failed|onButFanFail|offCooling|"
            "offConnectorRating|onButInlinePowerFail"
        )
        rules.append(
            _alert(
                "NetworkPowerFault",
                f'cefcFRUPowerOperStatus{{{scope},cefcFRUPowerOperStatus=~"{faults}"}} == 1',
                presets["power"],
                "Network device reports a power or cooling fault",
                labels,
            )
        )
    if presets["sensors"]["enabled"]:
        rules.append(
            _alert(
                "NetworkSensorFault",
                f"entSensorStatus{{{scope}}} == 3",
                presets["sensors"],
                "Network device reports a nonoperational sensor",
                labels,
            )
        )
    if presets["uptime_reset"]["enabled"]:
        # TimeTicks wrap at 2^32 hundredths of a second. Suppress near-wrap windows.
        expr = (
            f"resets(sysUpTime{{{scope}}}[15m]) > 0 and "
            f"max_over_time(sysUpTime{{{scope}}}[15m]) < {TIMETICKS_WRAP_GUARD}"
        )
        rules.append(
            _alert(
                "NetworkSNMPUptimeReset",
                expr,
                presets["uptime_reset"],
                "Network device or SNMP agent uptime reset",
                labels,
            )
        )
    return [{"name": f"alloy-network-devices-{owner}", "rules": rules}] + _interface_groups(
        scope, owner, settings, labels
    )


def _memory_expression(scope, threshold):
    expressions = []
    for prefix in ("cempMemPool", "ciscoMemoryPool"):
        used, free = f"{prefix}Used{{{scope}}}", f"{prefix}Free{{{scope}}}"
        expr = f"({used} / ({used} + {free}) * 100 > {threshold}) and ({used} + {free}) > 0"
        if prefix == "ciscoMemoryPool":
            expr += f" unless on({DEVICE_KEYS}) cempMemPoolUsed{{{scope}}}"
        expressions.append("(" + expr + ")")
    return " or ".join(expressions)


def _interface_groups(scope, owner, settings, labels):
    groups = []
    for item in settings.interfaces:
        selector = (
            scope + f",device=~{json.dumps(item.devices)},ifName=~{json.dumps(item.include)}"
        )
        if item.exclude:
            selector += f",ifName!~{json.dumps(item.exclude)}"
        interface_rules = _interface_rules(
            selector, settings.presets, {**labels, "network_alert_profile": item.name}
        )
        if interface_rules:
            groups.append(
                {"name": f"alloy-network-interfaces-{owner}-{item.name}", "rules": interface_rules}
            )
    return groups


def network_log_alert_groups(
    owner: str, *, topology_labels: Mapping[str, str], settings: NetworkAlertSettings
) -> list[dict[str, object]]:
    """Build opt-in LogQL rules for explicitly enabled parsing profiles."""
    if not settings.logs_enabled or not settings.parsers:
        return []
    scope = ",".join(
        ['log_source="remote_syslog"']
        + [
            f"{key}={json.dumps(topology_labels[key])}"
            for key in ("juju_model_uuid", "juju_application")
            if topology_labels.get(key)
        ]
    )
    events = {
        "link_changes": (
            "cisco_ios",
            "NetworkInterfaceEvents",
            'event_vendor="cisco" | event_type=~"interface_state|interface_flap"',
        ),
        "authentication_failures": (
            "cisco_ios",
            "NetworkAuthenticationFailures",
            'event_vendor="cisco" | event_type="authentication_failure"',
        ),
        "hardware_faults": (
            "cisco_ios",
            "NetworkHardwareAlarmEvents",
            'event_vendor="cisco" | event_type="hardware_alarm"',
        ),
        "configuration_changes": (
            "cisco_ios",
            "NetworkConfigurationChanged",
            'event_vendor="cisco" | event_type="configuration_change"',
        ),
        "firewall_blocks": (
            "opnsense_filterlog",
            "NetworkFirewallBlockEvents",
            'event_vendor="opnsense" | event_action="block"',
        ),
    }
    rules = []
    for name, (parser, alert_name, filters) in events.items():
        option = settings.logs[name]
        if parser not in settings.parsers or not option["enabled"]:
            continue
        expr = (
            "sum by (juju_model_uuid,juju_application,syslog_hostname,instance,event_interface) "
            f"(count_over_time({{{scope}}} | {filters} [{option['window']}])) >= {option['count']}"
        )
        rules.append(
            _alert(
                alert_name,
                expr,
                option,
                "Network log event threshold reached",
                {**topology_labels},
            )
        )
    return [{"name": f"alloy-network-logs-{owner}", "rules": rules}] if rules else []
