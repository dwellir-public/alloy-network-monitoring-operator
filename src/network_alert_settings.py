"""Validate public network alert presets and parser/publication controls."""

import math
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

import yaml

from network_syslog import parse_syslog_parsers

DEFAULT_PRESETS: dict[str, dict[str, Any]] = {
    "interface_down": {"enabled": True, "for": "5m"},
    "utilisation": {"enabled": True, "threshold": 85, "for": "10m"},
    "errors": {"enabled": True, "threshold": 1, "min_packets": 10, "for": "10m"},
    "discards": {"enabled": True, "threshold": 1, "min_packets": 10, "for": "10m"},
    "cpu": {"enabled": True, "threshold": 90, "for": "10m"},
    "memory": {"enabled": True, "threshold": 90, "for": "10m"},
    "power": {"enabled": True, "for": "2m"},
    "sensors": {"enabled": True, "for": "5m"},
    "uptime_reset": {"enabled": True, "for": "0s"},
}
DEFAULT_LOG_PRESETS: dict[str, dict[str, Any]] = {
    "link_changes": {"enabled": True, "count": 4, "window": "5m", "for": "1m"},
    "authentication_failures": {"enabled": True, "count": 5, "window": "5m", "for": "1m"},
    "hardware_faults": {"enabled": True, "count": 1, "window": "5m", "for": "1m"},
    "configuration_changes": {"enabled": False, "count": 1, "window": "5m", "for": "0s"},
    "firewall_blocks": {"enabled": False, "count": 1000, "window": "5m", "for": "5m"},
}


class NetworkAlertsError(ValueError):
    """Reject invalid public alert settings without reflecting their contents."""


class _UniqueLoader(yaml.SafeLoader):
    def construct_mapping(self, node, deep=False):
        pairs = self.construct_pairs(node, deep=deep)
        mapping = dict(pairs)
        if len(mapping) != len(pairs):
            raise NetworkAlertsError("network-alerts-config must not contain duplicate keys")
        return mapping


@dataclass(frozen=True)
class InterfaceScope:
    """One named expected-interface scope using Prometheus-compatible regexes."""

    name: str
    devices: str
    include: str
    exclude: str = ""


@dataclass(frozen=True)
class NetworkAlertSettings:
    """Resolved publication flags and public presets for one applied generation."""

    metrics_enabled: bool = True
    devices_enabled: bool = False
    logs_enabled: bool = False
    parsers: tuple[str, ...] = ()
    interfaces: tuple[InterfaceScope, ...] = ()
    presets: dict[str, dict[str, Any]] = field(
        default_factory=lambda: {name: dict(value) for name, value in DEFAULT_PRESETS.items()}
    )
    logs: dict[str, dict[str, Any]] = field(
        default_factory=lambda: {name: dict(value) for name, value in DEFAULT_LOG_PRESETS.items()}
    )


def _boolean(value: object, name: str) -> bool:
    if not isinstance(value, bool):
        raise NetworkAlertsError(f"{name} must be true or false")
    return value


def _duration(value: object, *, zero: bool = True) -> str:
    if (
        not isinstance(value, str)
        or len(value) > 64
        or not value
        or not re.fullmatch(
            r"(?:\d+y)?(?:\d+w)?(?:\d+d)?(?:\d+h)?(?:\d+m)?(?:\d+s)?(?:\d+ms)?", value
        )
    ):
        raise NetworkAlertsError("network-alerts-config durations must use units such as 5m")
    units = {"ms": 0.001, "s": 1, "m": 60, "h": 3600, "d": 86400, "w": 604800, "y": 31536000}
    seconds = sum(int(n) * units[u] for n, u in re.findall(r"(\d+)(ms|[smhdwy])", value))
    if seconds > 86400 or (not zero and seconds == 0):
        raise NetworkAlertsError("network-alerts-config durations must be bounded by 24h")
    return value


def _regex(value: object, *, nonempty: bool = False) -> str:
    if not isinstance(value, str) or len(value) > 128 or (nonempty and not value):
        raise NetworkAlertsError("network-alerts-config selectors require bounded regex strings")
    if (
        "(?" in value
        or re.search(r"\\(?:[1-9]|[AZGNuU])|(?:[*+?]|\})\+", value)
        or any(ord(c) < 32 for c in value)
        or any(int(n) > 1000 for n in re.findall(r"\{(\d+)(?:,\d*)?\}", value))
        or any(int(n) > 1000 for n in re.findall(r"\{\d+,(\d+)\}", value))
    ):
        raise NetworkAlertsError(
            "network-alerts-config selectors must use supported RE2 regex syntax"
        )
    try:
        re.compile(value)
    except (re.error, OverflowError):
        raise NetworkAlertsError("network-alerts-config contains an invalid regex") from None
    return value


def _preset(defaults: dict[str, Any], value: object) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) - set(defaults):
        raise NetworkAlertsError("network-alerts-config contains unsupported preset fields")
    result = {**defaults, **value}
    for key, item in result.items():
        if key == "enabled":
            _boolean(item, "network-alerts-config enabled")
        elif key in {"for", "window"}:
            _duration(item, zero=key == "for")
        elif (
            isinstance(item, bool)
            or not isinstance(item, (int, float))
            or item <= 0
            or item > 1_000_000
            or not math.isfinite(item)
        ):
            raise NetworkAlertsError(
                "network-alerts-config thresholds must be positive bounded numbers"
            )
        elif key == "threshold" and item > 100:
            raise NetworkAlertsError(
                "network-alerts-config percentage thresholds must be at most 100"
            )
        elif key == "count" and not isinstance(item, int):
            raise NetworkAlertsError("network-alerts-config event counts must be integers")
    return result


def parse_alert_settings(config: Mapping[str, object]) -> NetworkAlertSettings:
    """Resolve a strict bounded YAML schema and independent publication flags."""
    raw = config.get("network-alerts-config", "")
    if not isinstance(raw, str) or len(raw.encode()) > 16 * 1024:
        raise NetworkAlertsError("network-alerts-config must contain at most 16 KiB of YAML")
    try:
        data = yaml.load(raw, Loader=_UniqueLoader) if raw.strip() else {}
    except NetworkAlertsError:
        raise
    except (yaml.YAMLError, TypeError, ValueError, RecursionError):
        raise NetworkAlertsError("network-alerts-config must be valid YAML") from None
    if not isinstance(data, dict) or set(data) - (set(DEFAULT_PRESETS) | {"interfaces", "logs"}):
        raise NetworkAlertsError("network-alerts-config contains unsupported top-level fields")
    interfaces = data.get("interfaces", [])
    if not isinstance(interfaces, list) or len(interfaces) > 8:
        raise NetworkAlertsError("network-alerts-config supports at most 8 interface scopes")
    scopes = []
    names = set()
    for item in interfaces:
        if not isinstance(item, dict) or set(item) - {"name", "devices", "include", "exclude"}:
            raise NetworkAlertsError("network-alerts-config contains unsupported interface fields")
        name = item.get("name")
        if (
            not isinstance(name, str)
            or not re.fullmatch(r"[a-zA-Z0-9_-]{1,32}", name)
            or name in names
        ):
            raise NetworkAlertsError(
                "network-alerts-config interface names must be unique identifiers"
            )
        names.add(name)
        scopes.append(
            InterfaceScope(
                name,
                _regex(item.get("devices", ".+"), nonempty=True),
                _regex(item.get("include"), nonempty=True),
                _regex(item.get("exclude", "")),
            )
        )
    logs = data.get("logs", {})
    if not isinstance(logs, dict) or set(logs) - set(DEFAULT_LOG_PRESETS):
        raise NetworkAlertsError("network-alerts-config contains unsupported log presets")
    return NetworkAlertSettings(
        metrics_enabled=_boolean(
            config.get("network-alerts-enabled", True), "network-alerts-enabled"
        ),
        devices_enabled=_boolean(
            config.get("network-device-alerts-enabled", False), "network-device-alerts-enabled"
        ),
        logs_enabled=_boolean(
            config.get("network-log-alerts-enabled", False), "network-log-alerts-enabled"
        ),
        parsers=parse_syslog_parsers(config.get("syslog-parsers", "")),
        interfaces=tuple(scopes),
        presets={
            name: _preset(defaults, data.get(name, {}))
            for name, defaults in DEFAULT_PRESETS.items()
        },
        logs={
            name: _preset(defaults, logs.get(name, {}))
            for name, defaults in DEFAULT_LOG_PRESETS.items()
        },
    )
