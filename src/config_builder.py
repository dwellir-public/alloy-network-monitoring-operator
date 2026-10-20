#!/usr/bin/env python3
# Copyright 2025 Erik Lönroth
# See LICENSE file for licensing details.
"""Config builder for Alloy VM charm."""

from __future__ import annotations

import json
import os
import re
from collections.abc import Sequence
from dataclasses import dataclass, field

from network_monitoring import NetworkPipeline, render_network_blocks
from network_syslog import render_syslog_parser_stages
from outbound_endpoints import OutboundEndpoint

DEFAULT_CONFIG_DIR = "/etc/alloy"
DEFAULT_CONFIG_PATH = os.path.join(DEFAULT_CONFIG_DIR, "config.alloy")
DEFAULT_PACKAGE_CONFIG_BACKUP_PATH = os.path.join(
    DEFAULT_CONFIG_DIR, "config.alloy.package-default"
)
DEFAULT_CONFIG_BACKUP_PATH = os.path.join(DEFAULT_CONFIG_DIR, "config.alloy.bak")
REMOTE_WRITE_COMPONENT_NAME = "metrics"
REMOTE_WRITE_MAX_KEEPALIVE = "30m"
SYSLOG_STRUCTURED_METADATA = (
    "component",
    "connection_hostname",
    "receiver_hostname",
    "receiver_ip",
    "syslog_proc_id",
)
DEFAULT_SYSLOG_ACCESS_DROP_EXPRESSIONS = [
    '.*"(GET|POST|PUT|PATCH|DELETE|HEAD|OPTIONS|CONNECT|TRACE) .* HTTP/.*"',
]


class InvalidLogLevelError(ValueError):
    """Reject unsupported logging levels without echoing configuration."""


class InvalidSyslogPortError(ValueError):
    """Reject unusable RFC3164 listener ports before applying configuration."""


def normalize_log_level(value: str) -> str:
    """Preserve documented aliases while using Alloy's supported log levels."""
    levels = {
        "debug": "debug",
        "info": "info",
        "warn": "warn",
        "warning": "warn",
        "error": "error",
        "critical": "error",
    }
    try:
        return levels[value.strip().lower()]
    except (KeyError, AttributeError):
        raise InvalidLogLevelError(
            "log-level must be debug, info, warn, warning, error or critical"
        ) from None


@dataclass(frozen=True)
class ScrapeTarget:
    """One rendered Alloy scrape target."""

    address: str
    labels: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class MetricsScrapeJob:
    """A translated subset of a Prometheus scrape job."""

    job_name: str
    targets: list[ScrapeTarget]
    metrics_path: str = "/metrics"
    scheme: str = "http"
    scrape_interval: str = ""
    scrape_timeout: str = ""
    tls_config: dict[str, str | bool] = field(default_factory=dict)


@dataclass(frozen=True)
class FileLogSource:
    """One translated file log source."""

    include: list[str]
    exclude: list[str] = field(default_factory=list)
    attributes: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class LogSourceGroup:
    """One topology-labeled log source group."""

    component_name: str
    topology_labels: dict[str, str]
    systemd_units: list[str] = field(default_factory=list)
    journal_match_expressions: list[str] = field(default_factory=list)
    file_log_sources: list[FileLogSource] = field(default_factory=list)


class ConfigBuilder:
    """Alloy configuration builder class."""

    def __init__(
        self,
        *,
        loki_endpoints: Sequence[str | OutboundEndpoint],
        remote_write_endpoints: Sequence[str | OutboundEndpoint],
        metrics_scrape_jobs: list[MetricsScrapeJob],
        systemd_units: list[str],
        journal_kernel: bool,
        journal_match_expressions: list[str],
        live_debugging: bool = False,
        enable_syslog_receivers: bool = False,
        enable_syslog_rfc3164: bool = False,
        syslog_rfc3164_port: int = 1513,
        syslog_drop_access_logs: bool = False,
        syslog_drop_expressions: list[str] | None = None,
        syslog_rate_limit: int = 0,
        syslog_rate_burst: int = 0,
        receiver_hostname: str = "",
        receiver_ip: str = "",
        topology_labels: dict[str, str],
        log_source_groups: list[LogSourceGroup] | None = None,
        network_pipeline: NetworkPipeline | None = None,
        self_metrics_address: str = "127.0.0.1:6987",
        log_level: str = "info",
        collector_id: str = "",
        syslog_parsers: tuple[str, ...] = (),
        location_labels: dict[str, str] | None = None,
    ):
        self._loki_endpoints = loki_endpoints
        self._remote_write_endpoints = remote_write_endpoints
        self._metrics_scrape_jobs = metrics_scrape_jobs
        self._systemd_units = systemd_units
        self._journal_kernel = journal_kernel
        self._journal_match_expressions = journal_match_expressions
        self._live_debugging = live_debugging
        self._enable_syslog_receivers = enable_syslog_receivers
        self._enable_syslog_rfc3164 = enable_syslog_rfc3164
        self._syslog_rfc3164_port = syslog_rfc3164_port
        if enable_syslog_rfc3164:
            if not 1 <= syslog_rfc3164_port <= 65535:
                raise InvalidSyslogPortError("syslog-rfc3164-port must be between 1 and 65535")
            if enable_syslog_receivers and syslog_rfc3164_port == 1514:
                raise InvalidSyslogPortError(
                    "syslog-rfc3164-port conflicts with RFC5424 on port 1514"
                )
            if str(syslog_rfc3164_port) == self_metrics_address.rsplit(":", 1)[-1]:
                raise InvalidSyslogPortError(
                    "syslog-rfc3164-port conflicts with the Alloy HTTP listener"
                )
        self._syslog_drop_access_logs = syslog_drop_access_logs
        self._syslog_drop_expressions = syslog_drop_expressions or []
        self._syslog_rate_limit = syslog_rate_limit
        self._syslog_rate_burst = syslog_rate_burst
        self._receiver_hostname = receiver_hostname
        self._receiver_ip = receiver_ip
        self._syslog_parsers = syslog_parsers
        self._location_labels = location_labels or {}
        self._topology_labels = topology_labels
        self._log_source_groups = log_source_groups or []
        self._network_pipeline = network_pipeline
        self._self_metrics_address = self_metrics_address
        self._log_level = normalize_log_level(log_level)
        self._collector_id = (
            network_pipeline.settings.collector if network_pipeline else collector_id
        )

    @staticmethod
    def _normalize_endpoints(
        endpoints: Sequence[str | OutboundEndpoint],
    ) -> list[OutboundEndpoint]:
        """Return endpoint objects regardless of the input representation."""
        return [
            endpoint if isinstance(endpoint, OutboundEndpoint) else OutboundEndpoint(url=endpoint)
            for endpoint in endpoints
        ]

    def build(self) -> str:
        """Return the Alloy configuration text."""
        blocks = self._render_base_blocks()
        blocks.extend(self._render_metrics_blocks())
        blocks.extend(self._render_log_blocks())
        if self._network_pipeline and self._remote_write_endpoints:
            blocks.extend(
                [
                    "",
                    render_network_blocks(
                        self._network_pipeline, topology_labels=self._topology_labels
                    ),
                ]
            )
        return "\n".join(blocks).rstrip() + "\n"

    def _render_base_blocks(self) -> list[str]:
        return [
            self._render_logging(),
            "",
            self._render_unix_exporter(),
            "",
            self._render_local_metrics_relabel(),
            "",
            *([self._render_remote_write(), ""] if self._remote_write_endpoints else []),
            self._render_local_metrics_scrape(),
        ]

    def _render_metrics_blocks(self) -> list[str]:
        if not (self._remote_write_endpoints and self._metrics_scrape_jobs):
            return []
        blocks: list[str] = ["", "// METRICS -> REMOTE WRITE", ""]
        for scrape_job in self._metrics_scrape_jobs:
            blocks.extend([self._render_metrics_scrape(scrape_job), ""])
        return blocks

    def _render_log_blocks(self) -> list[str]:
        blocks: list[str] = []
        if self._live_debugging:
            blocks.extend([self._render_live_debugging(), ""])
        if self._has_log_pipeline():
            blocks.extend(["// LOGS -> LOKI (Juju topology labels)", ""])
        if self._has_labeled_log_sources() or self._has_host_journal_source():
            blocks.extend([self._render_journal_relabel(), ""])
        for group in self._effective_log_source_groups():
            blocks.extend([self._render_group_processor(group), ""])
            for block in self._render_group_journal_sources(group):
                blocks.extend([block, ""])
            for block in self._render_group_filelog_sources(group):
                blocks.extend([block, ""])
        for block in self._render_host_journal_sources():
            blocks.extend([block, ""])
        blocks.extend(self._render_syslog_blocks())
        if self._has_log_pipeline():
            blocks.append(self._render_loki_writer())
        return blocks

    def _render_syslog_blocks(self) -> list[str]:
        """Render shared processing once and each independently enabled receiver."""
        if not (self._enable_syslog_receivers or self._enable_syslog_rfc3164):
            return []
        blocks: list[str] = []
        blocks.extend(
            [
                self._render_syslog_relabel(),
                "",
                *self._render_syslog_processor_blocks(),
            ]
        )
        if self._enable_syslog_receivers:
            blocks.extend([self._render_syslog_source(), ""])
        if self._enable_syslog_rfc3164:
            blocks.extend(
                [
                    self._render_syslog_source(
                        name="receiver_rfc3164",
                        port=self._syslog_rfc3164_port,
                        syslog_format="rfc3164",
                    ),
                    "",
                ]
            )
        return blocks

    def _render_logging(self) -> str:
        return "\n".join(
            [
                "logging {",
                f"  level = {json.dumps(self._log_level)}",
                "}",
            ]
        )

    def _render_unix_exporter(self) -> str:
        return "\n".join(
            [
                'prometheus.exporter.unix "default" {',
                "  include_exporter_metrics = true",
                '  disable_collectors       = ["mdadm"]',
                "}",
            ]
        )

    def _render_local_metrics_relabel(self) -> str:
        rules = []
        if self._collector_id:
            rules.extend(
                [
                    "  rule {",
                    '    target_label = "collector"',
                    f"    replacement = {json.dumps(self._collector_id)}",
                    "  }",
                    "",
                ]
            )
        for key in self._topology_label_order():
            value = self._topology_labels.get(key)
            if value:
                rules.extend(
                    [
                        "  rule {",
                        f'    target_label = "{key}"',
                        f"    replacement  = {json.dumps(value)}",
                        "  }",
                        "",
                    ]
                )
        if rules:
            rules.pop()
        return "\n".join(
            [
                'discovery.relabel "local_metrics" {',
                "  targets = array.concat(",
                "    prometheus.exporter.unix.default.targets,",
                "    [{",
                '      job         = "alloy",',
                f"      __address__ = {json.dumps(self._self_metrics_address)},",
                "    }],",
                "  )",
                *(rules or [""]),
                "}",
            ]
        )

    def _render_local_metrics_scrape(self) -> str:
        return "\n".join(
            [
                'prometheus.scrape "default" {',
                "  targets    = discovery.relabel.local_metrics.output",
                '  job_name   = "alloy-local"',
                f"  forward_to = {self._metrics_forward_to()}",
                "}",
            ]
        )

    def _render_remote_write(self) -> str:
        endpoint_blocks = "\n".join(
            "\n".join(self._render_endpoint_block(endpoint))
            for endpoint in self._normalize_endpoints(self._remote_write_endpoints)
        )
        return "\n".join(
            [
                f'prometheus.remote_write "{REMOTE_WRITE_COMPONENT_NAME}" {{',
                *self._render_location_defaults(),
                endpoint_blocks,
                "",
                "  wal {",
                f'    max_keepalive_time = "{REMOTE_WRITE_MAX_KEEPALIVE}"',
                "  }",
                "}",
            ]
        )

    def _render_endpoint_block(self, endpoint: OutboundEndpoint) -> list[str]:
        """Render one Alloy endpoint block with optional auth and TLS CA."""
        lines = ["  endpoint {", f'    url = "{endpoint.url}"']
        if endpoint.username and endpoint.password:
            lines.extend(
                [
                    "    basic_auth {",
                    f"      username = {json.dumps(endpoint.username)}",
                    f"      password = {json.dumps(endpoint.password)}",
                    "    }",
                ]
            )
        if endpoint.tls_ca_pem:
            lines.extend(
                [
                    "    tls_config {",
                    f"      ca_pem = {json.dumps(endpoint.tls_ca_pem)}",
                    "    }",
                ]
            )
        lines.append("  }")
        return lines

    def _render_metrics_scrape(self, scrape_job: MetricsScrapeJob) -> str:
        component_name = self._sanitize_component_name(scrape_job.job_name)
        lines = [
            f'prometheus.scrape "{component_name}" {{',
            "  targets = [",
            *self._render_targets(scrape_job.targets),
            "  ]",
            f"  job_name = {json.dumps(scrape_job.job_name)}",
        ]
        if scrape_job.metrics_path:
            lines.append(f"  metrics_path = {json.dumps(scrape_job.metrics_path)}")
        if scrape_job.scheme:
            lines.append(f"  scheme = {json.dumps(scrape_job.scheme)}")
        if scrape_job.scrape_interval:
            lines.append(f"  scrape_interval = {json.dumps(scrape_job.scrape_interval)}")
        if scrape_job.scrape_timeout:
            lines.append(f"  scrape_timeout = {json.dumps(scrape_job.scrape_timeout)}")
        if scrape_job.tls_config:
            lines.extend(self._render_tls_config(scrape_job.tls_config))
        lines.append(f"  forward_to = {self._metrics_forward_to()}")
        lines.append("}")
        return "\n".join(lines)

    def _render_tls_config(self, tls_config: dict[str, str | bool]) -> list[str]:
        lines = ["  tls_config {"]
        for key in sorted(tls_config):
            value = tls_config[key]
            if isinstance(value, bool):
                rendered_value = "true" if value else "false"
            else:
                rendered_value = json.dumps(value)
            lines.append(f"    {self._render_key(key)} = {rendered_value}")
        lines.append("  }")
        return lines

    def _render_targets(self, targets: list[ScrapeTarget]) -> list[str]:
        rendered: list[str] = []
        for target in targets:
            rendered.extend(
                [
                    "    {",
                    f'      __address__ = "{target.address}",',
                    *self._render_target_labels(self._target_topology(target.labels)),
                    "    },",
                ]
            )
        return rendered

    def _target_topology(self, labels: dict[str, str]) -> dict[str, str]:
        """Add collector context to external targets, preserving provider ownership."""
        if any(labels.get(key) for key in self._topology_label_order()):
            return labels
        return {**self._topology_labels, **labels}

    def _render_target_labels(self, labels: dict[str, str]) -> list[str]:
        lines = []
        for key in sorted(labels):
            lines.append(f"      {self._render_key(key)} = {json.dumps(labels[key])},")
        return lines

    def _render_live_debugging(self) -> str:
        return "\n".join(
            [
                "livedebugging {",
                "  enabled = true",
                "}",
            ]
        )

    def _render_service_journal_sources(self) -> list[str]:
        return []

    def _render_host_journal_sources(self) -> list[str]:
        forward_to = (
            "  forward_to = [loki.write.main.receiver]"
            if self._loki_endpoints
            else "  forward_to = []"
        )
        blocks: list[str] = []
        host_matches = self._host_journal_matches()
        for index, match in enumerate(host_matches):
            component_name = (
                "host_journald" if len(host_matches) == 1 else f"host_journald_{index}"
            )
            blocks.append(
                "\n".join(
                    [
                        f'loki.source.journal "{component_name}" {{',
                        f'  matches = "{match}"',
                        "  relabel_rules = loki.relabel.journal.rules",
                        "  labels = "
                        + self._render_inline_labels(
                            {**self._topology_labels, "log_source": "journal"}
                        ),
                        forward_to,
                        "}",
                    ]
                )
            )
        return blocks

    def _render_journal_relabel(self) -> str:
        return "\n".join(
            [
                'loki.relabel "journal" {',
                "  forward_to = []",
                "",
                "  rule {",
                '    source_labels = ["__journal__systemd_unit"]',
                '    target_label  = "systemd_unit"',
                "  }",
                "",
                "  rule {",
                '    source_labels = ["__journal_syslog_identifier"]',
                '    target_label  = "syslog_identifier"',
                "  }",
                "",
                "  rule {",
                '    source_labels = ["__journal_priority_keyword"]',
                '    target_label  = "level"',
                "  }",
                "",
                "  rule {",
                '    source_labels = ["__journal_priority"]',
                '    target_label  = "severity"',
                "  }",
                "}",
            ]
        )

    def _render_syslog_relabel(self) -> str:
        return "\n".join(
            [
                'loki.relabel "syslog" {',
                "  forward_to = []",
                "",
                "  rule {",
                '    source_labels = ["__syslog_connection_ip_address"]',
                '    target_label  = "source_ip"',
                "  }",
                "",
                "  rule {",
                '    source_labels = ["__syslog_message_hostname"]',
                '    target_label  = "syslog_hostname"',
                "  }",
                "",
                "  rule {",
                '    source_labels = ["__syslog_connection_hostname"]',
                '    target_label  = "connection_hostname"',
                "  }",
                "",
                "  rule {",
                '    source_labels = ["__syslog_message_app_name"]',
                '    target_label  = "syslog_app_name"',
                "  }",
                "",
                "  rule {",
                '    source_labels = ["__syslog_message_facility"]',
                '    target_label  = "syslog_facility"',
                "  }",
                "",
                "  rule {",
                '    source_labels = ["__syslog_message_proc_id"]',
                '    target_label  = "syslog_proc_id"',
                "  }",
                "",
                "  rule {",
                '    source_labels = ["__syslog_message_severity"]',
                '    target_label  = "level"',
                "  }",
                "",
                "  rule {",
                '    source_labels = ["__syslog_message_severity"]',
                '    target_label  = "severity"',
                "  }",
                "}",
            ]
        )

    def _render_syslog_source(
        self, *, name: str = "receiver", port: int = 1514, syslog_format: str = "rfc5424"
    ) -> str:
        """Render independent TCP/UDP listeners sharing syslog processing and identity."""
        receiver_hostname = self._receiver_hostname or "unknown"
        receiver_ip = self._receiver_ip or "unknown"
        labels = {
            **self._topology_labels,
            "component": "loki.source.syslog",
            "service_name": "loki.source.syslog",
            "log_source": "remote_syslog",
            "receiver_hostname": receiver_hostname,
            "receiver_ip": receiver_ip,
        }
        forward_to = (
            "  forward_to = [loki.process.remote_syslog.receiver]"
            if self._loki_endpoints
            else "  forward_to = []"
        )
        return "\n".join(
            [
                f'loki.source.syslog "{name}" {{',
                "  relabel_rules = loki.relabel.syslog.rules",
                "",
                "  listener {",
                f'    address = ":{port}"',
                *(['    syslog_format = "rfc3164"'] if syslog_format == "rfc3164" else []),
                "    labels = " + self._render_inline_labels({**labels, "protocol": "tcp"}),
                "  }",
                "",
                "  listener {",
                f'    address  = ":{port}"',
                '    protocol = "udp"',
                *(['    syslog_format = "rfc3164"'] if syslog_format == "rfc3164" else []),
                "    labels = " + self._render_inline_labels({**labels, "protocol": "udp"}),
                "  }",
                "",
                forward_to,
                "}",
            ]
        )

    def _render_syslog_processor_blocks(self) -> list[str]:
        if not self._loki_endpoints:
            return []
        return [self._render_remote_syslog_processor(), ""]

    def _render_remote_syslog_processor(self) -> str:
        metadata: list[str] = list(SYSLOG_STRUCTURED_METADATA)
        if len(self._location_labels) == 2:
            metadata.append("protocol")
        lines = ['loki.process "remote_syslog" {']
        lines.extend(
            [
                "  stage.structured_metadata {",
                "    values = {",
                *(f'      {key} = "",' for key in metadata),
                "    }",
                "  }",
                "",
            ]
        )
        lines.extend(render_syslog_parser_stages(self._syslog_parsers))
        for expression in self._effective_syslog_drop_expressions():
            lines.extend(
                [
                    "  stage.drop {",
                    f"    expression = {json.dumps(expression)}",
                    "  }",
                    "",
                ]
            )
        if self._should_render_syslog_rate_limit():
            lines.extend(
                [
                    "  stage.limit {",
                    f"    rate = {self._syslog_rate_limit}",
                    f"    burst = {self._effective_syslog_rate_burst()}",
                    "    drop = true",
                    "  }",
                    "",
                ]
            )
        lines.extend(
            [
                "  forward_to = [loki.write.main.receiver]",
                "}",
            ]
        )
        return "\n".join(lines)

    def _render_group_processor(self, group: LogSourceGroup) -> str:
        forward_to = (
            "  forward_to = [loki.write.main.receiver]"
            if self._loki_endpoints
            else "  forward_to = []"
        )
        component_name = self._sanitize_component_name(group.component_name)
        return "\n".join(
            [
                f'loki.process "{component_name}" {{',
                "  stage.static_labels {",
                "    values = {",
                "\n".join(self._render_label_lines(group.topology_labels, indent="      ")),
                "    }",
                "  }",
                forward_to,
                "}",
            ]
        )

    def _render_topology_labels(self) -> list[str]:
        lines = []
        for key in self._topology_label_order():
            value = self._topology_labels.get(key)
            if value:
                lines.append(f'      {key} = "{value}",')
        return lines or ["      {}"]

    def _render_label_lines(self, labels: dict[str, str], *, indent: str) -> list[str]:
        """Render a stable block of Alloy object-label assignments."""
        return [
            f"{indent}{self._render_key(key)} = {json.dumps(labels[key])},"
            for key in sorted(labels)
        ]

    def _render_inline_labels(self, labels: dict[str, str]) -> str:
        """Render a stable, safely quoted inline label map."""
        fields = (f"{self._render_key(key)} = {json.dumps(labels[key])}" for key in sorted(labels))
        return "{" + ", ".join(fields) + "}"

    def _render_loki_writer(self) -> str:
        if not self._loki_endpoints:
            return ""
        endpoints = "\n".join(
            "\n".join(self._render_endpoint_block(endpoint))
            for endpoint in self._normalize_endpoints(self._loki_endpoints)
        )
        return "\n".join(
            [
                "",
                'loki.write "main" {',
                *self._render_location_defaults(),
                endpoints,
                "}",
            ]
        )

    def _render_location_defaults(self) -> list[str]:
        """Add collection location without replacing existing source label values."""
        if not self._location_labels:
            return []
        return ["  external_labels = " + self._render_inline_labels(self._location_labels)]

    def _metrics_forward_to(self) -> str:
        if not self._remote_write_endpoints:
            return "[]"
        return f"[prometheus.remote_write.{REMOTE_WRITE_COMPONENT_NAME}.receiver]"

    def _has_host_journal_source(self) -> bool:
        return bool(self._host_journal_matches())

    def _has_labeled_log_sources(self) -> bool:
        return bool(self._effective_log_source_groups())

    def _has_log_pipeline(self) -> bool:
        return bool(
            self._effective_log_source_groups()
            or self._has_host_journal_source()
            or self._enable_syslog_receivers
            or self._enable_syslog_rfc3164
        )

    def _host_journal_matches(self) -> list[str]:
        matches: list[str] = []
        if self._journal_kernel:
            matches.append("_TRANSPORT=kernel")
        matches.extend(self._journal_match_expressions)
        return matches

    def _effective_syslog_drop_expressions(self) -> list[str]:
        expressions: list[str] = []
        if self._syslog_drop_access_logs:
            expressions.extend(DEFAULT_SYSLOG_ACCESS_DROP_EXPRESSIONS)
        expressions.extend(self._syslog_drop_expressions)
        return expressions

    def _should_render_syslog_rate_limit(self) -> bool:
        return self._syslog_rate_limit > 0

    def _effective_syslog_rate_burst(self) -> int:
        if self._syslog_rate_burst > 0:
            return self._syslog_rate_burst
        return self._syslog_rate_limit

    @staticmethod
    def _group_journal_source_name(
        *, component_name: str, source_kind: str, index: int, total: int
    ) -> str:
        if component_name == "juju":
            return source_kind if total == 1 else f"{source_kind}_{index}"
        return (
            f"{component_name}_{source_kind}"
            if total == 1
            else f"{component_name}_{source_kind}_{index}"
        )

    def _effective_log_source_groups(self) -> list[LogSourceGroup]:
        groups = []
        if self._systemd_units:
            groups.append(
                LogSourceGroup(
                    component_name="juju",
                    topology_labels=self._topology_labels,
                    systemd_units=list(self._systemd_units),
                )
            )
        groups.extend(self._log_source_groups)
        return groups

    def _render_group_journal_sources(self, group: LogSourceGroup) -> list[str]:
        component_name = self._sanitize_component_name(group.component_name)
        blocks: list[str] = []
        for index, unit in enumerate(group.systemd_units):
            source_name = self._group_journal_source_name(
                component_name=component_name,
                source_kind="journald",
                index=index,
                total=len(group.systemd_units),
            )
            blocks.append(
                "\n".join(
                    [
                        f'loki.source.journal "{source_name}" {{',
                        f'  matches = "{self._format_unit_match(unit)}"',
                        "  relabel_rules = loki.relabel.journal.rules",
                        f'  labels = {{log_source = "journal", systemd_unit = "{unit}"}}',
                        f"  forward_to = [loki.process.{component_name}.receiver]",
                        "}",
                    ]
                )
            )
        for index, match in enumerate(group.journal_match_expressions):
            source_name = self._group_journal_source_name(
                component_name=component_name,
                source_kind="journal_match",
                index=index,
                total=len(group.journal_match_expressions),
            )
            blocks.append(
                "\n".join(
                    [
                        f'loki.source.journal "{source_name}" {{',
                        f'  matches = "{match}"',
                        "  relabel_rules = loki.relabel.journal.rules",
                        '  labels = {log_source = "journal"}',
                        f"  forward_to = [loki.process.{component_name}.receiver]",
                        "}",
                    ]
                )
            )
        return blocks

    def _render_group_filelog_sources(self, group: LogSourceGroup) -> list[str]:
        if not group.file_log_sources:
            return []
        component_name = self._sanitize_component_name(group.component_name)
        matcher_name = f"{component_name}_filelogs"
        return [
            "\n".join(
                [
                    f'local.file_match "{matcher_name}" {{',
                    "  path_targets = [",
                    *self._render_file_targets(group.file_log_sources),
                    "  ]",
                    "}",
                ]
            ),
            "\n".join(
                [
                    f'loki.source.file "{matcher_name}" {{',
                    f"  targets    = local.file_match.{matcher_name}.targets",
                    f"  forward_to = [loki.process.{component_name}.receiver]",
                    "}",
                ]
            ),
        ]

    def _render_file_targets(self, file_log_sources: list[FileLogSource]) -> list[str]:
        rendered: list[str] = []
        for source in file_log_sources:
            for include in source.include:
                rendered.extend(
                    [
                        "    {",
                        f'      __path__ = "{include}",',
                        *(
                            [
                                "      __path_exclude__ = "
                                f'"{self._combine_excludes(source.exclude)}",'
                            ]
                            if source.exclude
                            else []
                        ),
                        *self._render_label_lines(source.attributes, indent="      "),
                        "    },",
                    ]
                )
        return rendered

    @staticmethod
    def _sanitize_component_name(name: str) -> str:
        sanitized = re.sub(r"[^a-zA-Z0-9_]+", "_", name).strip("_").lower()
        return sanitized or "metrics"

    @staticmethod
    def _combine_excludes(excludes: list[str]) -> str:
        return ",".join(excludes)

    @staticmethod
    def _render_key(key: str) -> str:
        if re.match(r"^[A-Za-z_][A-Za-z0-9_]*$", key):
            return key
        return json.dumps(key)

    @staticmethod
    def _format_unit_match(value: str) -> str:
        return f"_SYSTEMD_UNIT={value}"

    @staticmethod
    def _topology_label_order() -> list[str]:
        return [
            "juju_model",
            "juju_model_uuid",
            "juju_application",
            "juju_unit",
            "juju_charm",
        ]
