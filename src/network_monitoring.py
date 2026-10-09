"""Validate network monitoring inputs and render the native Alloy pipeline."""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from urllib.parse import urlencode, urlsplit, urlunsplit

import yaml

_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")
_SLUG = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9_-]{0,99}$")
_DURATION = re.compile(r"(?:\d+(?:ms|[smhdwy]))+")
_SECONDS = {"ms": 0.001, "s": 1, "m": 60, "h": 3600, "d": 86400, "w": 604800, "y": 31536000}
_IPV4_OCTET = r"(25[0-5]|2[0-4][0-9]|1[0-9]{2}|[1-9]?[0-9])"
_IPV4 = rf"{_IPV4_OCTET}(\.{_IPV4_OCTET}){{3}}"
_PROFILE_FIELDS = {
    "version",
    "username",
    "security_level",
    "password",
    "auth_protocol",
    "priv_protocol",
    "priv_password",
    "context_name",
}
_AUTH_PROTOCOLS = {"MD5", "SHA", "SHA224", "SHA256", "SHA384", "SHA512"}
_PRIV_PROTOCOLS = {"DES", "AES", "AES192", "AES256", "AES192C", "AES256C"}
MAX_AUTH_BYTES = 1024 * 1024
MAX_AUTH_PROFILES = 128


class NetworkMonitoringError(ValueError):
    """Describe an invalid public setting or secret without exposing its values."""


@dataclass(frozen=True)
class NetworkSettings:
    """Public settings for one collector, independent of its optional filters."""

    discovery_url: str
    token_secret: str
    snmp_secret: str
    collector: str
    site: str = ""
    region: str = ""
    tags: tuple[str, ...] = ()
    status: str = "active"
    auth_scheme: str = "Bearer"
    refresh_interval: str = "60s"
    scrape_interval: str = "30s"
    scrape_timeout: str = "15s"
    ca_secret: str = ""


@dataclass(frozen=True)
class SnmpAuthentications:
    """A validated set of named profiles with secret-bearing YAML hidden in repr."""

    names: tuple[str, ...]
    yaml_text: str = field(repr=False)


@dataclass(frozen=True)
class NetworkPipeline:
    """Rendering inputs containing public values and protected file references."""

    settings: NetworkSettings
    profiles: tuple[str, ...]
    token_file: str
    snmp_file: str
    ca_file: str = ""


def _string(config: Mapping[str, object], key: str, default: str = "") -> str:
    """Read a string setting without including its contents in an error."""
    value = config.get(key, default)
    if not isinstance(value, str):
        raise NetworkMonitoringError(f"{key} must be a string")
    return value.strip()


def _slug(config: Mapping[str, object], key: str) -> str:
    """Accept an optional exact NetBox slug."""
    value = _string(config, key)
    if value and not _SLUG.fullmatch(value):
        raise NetworkMonitoringError(f"{key} must be a NetBox slug")
    return value


def _duration(config: Mapping[str, object], key: str, default: str) -> tuple[str, float]:
    """Accept a positive Prometheus duration and return its duration in seconds."""
    value = _string(config, key, default)
    if not _DURATION.fullmatch(value):
        raise NetworkMonitoringError(f"{key} must be a positive duration, such as 30s")
    seconds = sum(
        int(number) * _SECONDS[unit] for number, unit in re.findall(r"(\d+)(ms|[smhdwy])", value)
    )
    if seconds <= 0:
        raise NetworkMonitoringError(f"{key} must be greater than zero")
    return value, seconds


def _discovery_url(base: str, filters: list[tuple[str, str]]) -> str:
    """Append the plugin devices path and encoded filters to a NetBox base URL."""
    try:
        parsed = urlsplit(base)
        port = parsed.port
    except ValueError:
        raise NetworkMonitoringError("netbox-url must be an absolute HTTP(S) base URL") from None
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
        or port == 0
    ):
        raise NetworkMonitoringError(
            "netbox-url must be an HTTP(S) base URL without credentials or a query"
        )
    path = parsed.path.rstrip("/") + "/api/plugins/prometheus-sd/devices/"
    return urlunsplit((parsed.scheme, parsed.netloc, path, urlencode(filters), ""))


def parse_network_settings(config: Mapping[str, object]) -> NetworkSettings | None:
    """Resolve enabled settings; empty site, region and tags impose no filter."""
    if not config.get("network-monitoring-enabled", False) or _string(config, "config-override"):
        return None
    site = _slug(config, "netbox-site")
    region = _slug(config, "netbox-region")
    tags = tuple(
        sorted({tag.strip() for tag in _string(config, "netbox-tags").split(",") if tag.strip()})
    )
    if any(not _SLUG.fullmatch(tag) for tag in tags):
        raise NetworkMonitoringError("netbox-tags must contain comma-separated NetBox tag slugs")
    status = _string(config, "netbox-status", "active")
    if status and not _SLUG.fullmatch(status):
        raise NetworkMonitoringError("netbox-status must be a status slug or empty")
    collector = _string(config, "collector-id")
    if not _NAME.fullmatch(collector):
        raise NetworkMonitoringError(
            "collector-id is required and must use letters, numbers, dots, dashes or underscores"
        )
    scheme = _string(config, "netbox-auth-scheme", "Bearer")
    if scheme not in {"Bearer", "Token"}:
        raise NetworkMonitoringError("netbox-auth-scheme must be Bearer or Token")
    refresh, _ = _duration(config, "netbox-refresh-interval", "60s")
    interval, interval_seconds = _duration(config, "snmp-scrape-interval", "30s")
    timeout, timeout_seconds = _duration(config, "snmp-scrape-timeout", "15s")
    if timeout_seconds >= interval_seconds:
        raise NetworkMonitoringError("snmp-scrape-timeout must be less than snmp-scrape-interval")
    filters = [
        (key, value)
        for key, value in (("status", status), ("site", site), ("region", region))
        if value
    ]
    filters.extend(("tag", tag) for tag in tags)
    return NetworkSettings(
        discovery_url=_discovery_url(_string(config, "netbox-url"), filters),
        token_secret=_string(config, "netbox-token"),
        snmp_secret=_string(config, "snmp-auth"),
        ca_secret=_string(config, "netbox-ca"),
        collector=collector,
        site=site,
        region=region,
        tags=tags,
        status=status,
        auth_scheme=scheme,
        refresh_interval=refresh,
        scrape_interval=interval,
        scrape_timeout=timeout,
    )


class _UniqueKeyLoader(yaml.SafeLoader):
    """Reject duplicated YAML keys instead of silently replacing credentials."""

    def construct_mapping(self, node, deep=False):
        """Construct a mapping with distinct, scalar keys."""
        self.flatten_mapping(node)
        keys = []
        for key_node, _ in node.value:
            key = self.construct_object(key_node, deep=deep)
            if not isinstance(key, str) or key in keys:
                raise NetworkMonitoringError("snmp-auth YAML must contain unique string keys")
            keys.append(key)
        return super().construct_mapping(node, deep=deep)


def _profile_string(profile: Mapping[str, object], key: str, *, required: bool = False) -> str:
    """Validate a profile field without echoing secret contents."""
    value = profile.get(key, "")
    if not isinstance(value, str) or (required and not value.strip()) or "\x00" in value:
        raise NetworkMonitoringError(
            f"snmp-auth profile field {key} must be a non-empty string"
            if required
            else f"snmp-auth profile field {key} must be a string"
        )
    return value


def _validate_profile(profile: object) -> None:
    """Accept only the supported SNMPv3 authentication schema."""
    if not isinstance(profile, dict) or set(profile) - _PROFILE_FIELDS:
        raise NetworkMonitoringError(
            "snmp-auth profiles must use only supported SNMPv3 authentication fields"
        )
    if type(profile.get("version")) is not int or profile["version"] != 3:
        raise NetworkMonitoringError("snmp-auth profiles must explicitly set version: 3")
    _profile_string(profile, "username", required=True)
    level = _profile_string(profile, "security_level", required=True)
    if level not in {"noAuthNoPriv", "authNoPriv", "authPriv"}:
        raise NetworkMonitoringError(
            "snmp-auth security_level must be noAuthNoPriv, authNoPriv or authPriv"
        )
    for key in set(profile) - {"version", "username", "security_level"}:
        _profile_string(profile, key)
    if level != "noAuthNoPriv":
        if profile.get("auth_protocol") not in _AUTH_PROTOCOLS:
            raise NetworkMonitoringError(
                "snmp-auth profiles must select a supported auth_protocol"
            )
        if len(_profile_string(profile, "password", required=True)) < 8:
            raise NetworkMonitoringError(
                "snmp-auth authentication passphrases must contain at least 8 characters"
            )
    if level == "authPriv":
        _validate_privacy(profile)


def _validate_privacy(profile: Mapping[str, object]) -> None:
    """Check the required privacy algorithm and passphrase."""
    if profile.get("priv_protocol") not in _PRIV_PROTOCOLS:
        raise NetworkMonitoringError("snmp-auth profiles must select a supported priv_protocol")
    if len(_profile_string(profile, "priv_password", required=True)) < 8:
        raise NetworkMonitoringError(
            "snmp-auth privacy passphrases must contain at least 8 characters"
        )


def parse_snmp_authentications(content: Mapping[str, str]) -> SnmpAuthentications:
    """Validate multiple named profiles from the Juju secret's snmp-yaml field."""
    raw = content.get("snmp-yaml", "")
    if not isinstance(raw, str) or not raw or len(raw.encode()) > MAX_AUTH_BYTES:
        raise NetworkMonitoringError(
            "snmp-auth secret must contain a non-empty snmp-yaml field of at most 1 MiB"
        )
    try:
        parsed = yaml.load(raw, Loader=_UniqueKeyLoader)
    except (yaml.YAMLError, RecursionError):
        raise NetworkMonitoringError("snmp-auth snmp-yaml must be valid YAML") from None
    if not isinstance(parsed, dict) or set(parsed) != {"auths"}:
        raise NetworkMonitoringError("snmp-auth snmp-yaml must contain only an auths mapping")
    profiles = parsed["auths"]
    if not isinstance(profiles, dict) or not 1 <= len(profiles) <= MAX_AUTH_PROFILES:
        raise NetworkMonitoringError(
            "snmp-auth auths must contain between 1 and 128 named profiles"
        )
    for name, profile in profiles.items():
        if not isinstance(name, str) or not _NAME.fullmatch(name):
            raise NetworkMonitoringError(
                "snmp-auth profile names must use letters, numbers, dots, dashes or underscores"
            )
        _validate_profile(profile)
    return SnmpAuthentications(tuple(sorted(profiles)), yaml.safe_dump(parsed, sort_keys=True))


def parse_netbox_token(content: Mapping[str, str]) -> str:
    """Read the token without accepting an injected header or logging its value."""
    value = content.get("token", "")
    if isinstance(value, str):
        value = value.strip()
    if (
        not isinstance(value, str)
        or not value.strip()
        or any(char in value for char in "\r\n\x00")
    ):
        raise NetworkMonitoringError(
            "netbox-token secret must contain a non-empty single-line token field"
        )
    return value.strip()


def _rule(
    *,
    source: str = "",
    target: str = "",
    replacement: str = "",
    action: str = "replace",
    regex: str = "",
) -> str:
    """Render a relabel rule with safely quoted string values."""
    fields = [f"    action = {json.dumps(action)}"]
    if source:
        fields.append(f"    source_labels = [{json.dumps(source)}]")
    for key, value in (("target_label", target), ("replacement", replacement), ("regex", regex)):
        if value:
            fields.append(f"    {key} = {json.dumps(value)}")
    return "  rule {\n" + "\n".join(fields) + "\n  }"


def render_network_blocks(pipeline: NetworkPipeline) -> str:
    """Render discovery, explicit inventory normalization, SNMP export and scraping."""
    settings = pipeline.settings
    rules = [
        _rule(source="__meta_netbox_id", action="keep", regex="[1-9][0-9]*"),
        _rule(source="__meta_netbox_name", action="keep", regex=".+"),
        _rule(source="__meta_netbox_primary_ip4", action="keep", regex=_IPV4),
        _rule(
            source="__meta_netbox_custom_field_snmp_auth",
            action="keep",
            regex="(" + "|".join(re.escape(name) for name in pipeline.profiles) + ")",
        ),
    ]
    for field_name, value in (("site_slug", settings.site), ("status", settings.status)):
        if value:
            rules.append(
                _rule(source=f"__meta_netbox_{field_name}", action="keep", regex=re.escape(value))
            )
    for tag in settings.tags:
        rules.append(
            _rule(
                source="__meta_netbox_tag_slugs",
                action="keep",
                regex=rf"([^,]+,)*{re.escape(tag)}(,[^,]+)*",
            )
        )
    for source, target in (
        ("id", "netbox_id"),
        ("name", "device"),
        ("site_slug", "site"),
        ("primary_ip4", "address"),
        ("custom_field_snmp_auth", "auth"),
    ):
        rules.append(_rule(source=f"__meta_netbox_{source}", target=target))
    rules.extend(
        [
            _rule(source="netbox_id", target="name", replacement="netbox-device-${1}"),
            _rule(target="module", replacement="system,if_mib"),
            _rule(target="collector", replacement=settings.collector),
            _rule(
                action="labelkeep",
                regex="name|address|module|auth|netbox_id|device|site|collector",
            ),
        ]
    )
    tls = (
        f"  tls_config {{\n    ca_file = {json.dumps(pipeline.ca_file)}\n  }}\n"
        if pipeline.ca_file
        else ""
    )
    return "\n".join(
        [
            'local.file "network_netbox_token" {',
            f"  filename = {json.dumps(pipeline.token_file)}",
            "  is_secret = true",
            "}",
            "",
            'local.file "network_snmp_auth" {',
            f"  filename = {json.dumps(pipeline.snmp_file)}",
            "  is_secret = true",
            "}",
            "",
            'discovery.http "network_netbox" {',
            f"  url = {json.dumps(settings.discovery_url)}",
            f"  refresh_interval = {json.dumps(settings.refresh_interval)}",
            "  authorization {",
            f"    type = {json.dumps(settings.auth_scheme)}",
            "    credentials = local.file.network_netbox_token.content",
            "  }",
            tls + "}",
            "",
            'discovery.relabel "network_inventory" {',
            "  targets = discovery.http.network_netbox.targets",
            *rules,
            "}",
            "",
            'prometheus.exporter.snmp "network" {',
            "  config = local.file.network_snmp_auth.content",
            '  config_merge_strategy = "merge"',
            "  targets = discovery.relabel.network_inventory.output",
            "}",
            "",
            'discovery.relabel "network_scrape" {',
            "  targets = prometheus.exporter.snmp.network.targets",
            _rule(target="job", replacement="snmp/network"),
            _rule(source="netbox_id", target="instance", replacement="netbox-device-${1}"),
            _rule(action="labelkeep", regex="__.*|job|instance|netbox_id|device|site|collector"),
            "}",
            "",
            'prometheus.scrape "network_snmp" {',
            "  targets = discovery.relabel.network_scrape.output",
            '  job_name = "snmp/network"',
            f"  scrape_interval = {json.dumps(settings.scrape_interval)}",
            f"  scrape_timeout = {json.dumps(settings.scrape_timeout)}",
            "  forward_to = [prometheus.remote_write.metrics.receiver]",
            "}",
        ]
    )
