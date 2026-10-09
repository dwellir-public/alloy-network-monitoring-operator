#!/usr/bin/env python3
# Copyright 2026 Erik Lönroth
# See LICENSE file for licensing details.
"""Helpers for manual operator-defined metrics scrape jobs."""

from __future__ import annotations

import re
import socket
import textwrap
from collections.abc import Mapping

import yaml

from config_builder import MetricsScrapeJob, ScrapeTarget

_DURATION_PATTERN = re.compile(r"^\d+[ywdhms]$")
_RESERVED_LABEL_PREFIX = "juju_"


class ManualMetricsJobsError(ValueError):
    """Raised when manual metrics job config is malformed.

    The strategy is to reject the whole config with a precise operator-facing
    message instead of silently applying a partial subset.
    """


def parse_manual_metrics_jobs(
    raw_config: str,
    *,
    topology_labels: Mapping[str, str],
) -> list[MetricsScrapeJob]:
    """Parse the manual metrics jobs YAML into rendered scrape jobs.

    The release-1 schema is intentionally narrow and Alloy-native so the charm
    can validate it completely before rendering managed config.
    """
    raw_config = textwrap.dedent(raw_config).strip()
    if not raw_config:
        return []

    try:
        parsed = yaml.safe_load(raw_config)
    except yaml.YAMLError as exc:
        raise ManualMetricsJobsError(f"manual-metrics-jobs must be valid YAML: {exc}") from exc

    if parsed is None:
        return []
    if not isinstance(parsed, list):
        raise ManualMetricsJobsError("manual-metrics-jobs must be a YAML list of jobs")

    jobs: list[MetricsScrapeJob] = []
    seen_names: set[str] = set()
    for index, raw_job in enumerate(parsed):
        if not isinstance(raw_job, Mapping):
            raise ManualMetricsJobsError(
                f"manual-metrics-jobs entry {index + 1} must be a mapping"
            )

        name = _required_string_field(raw_job, "name")
        if name in seen_names:
            raise ManualMetricsJobsError(
                f"manual metrics job names must be unique; duplicate name '{name}'"
            )
        seen_names.add(name)
        jobs.append(
            _build_metrics_scrape_job(
                name=name,
                raw_job=raw_job,
                topology_labels=topology_labels,
            )
        )
    return jobs


def _build_metrics_scrape_job(
    *,
    name: str,
    raw_job: Mapping[str, object],
    topology_labels: Mapping[str, str],
) -> MetricsScrapeJob:
    """Validate one manual job and translate it into the builder model."""
    supported_fields = {
        "name",
        "targets",
        "metrics_path",
        "scheme",
        "scrape_interval",
        "scrape_timeout",
        "insecure_skip_verify",
        "labels",
    }
    unsupported_fields = sorted(set(raw_job) - supported_fields)
    if unsupported_fields:
        raise ManualMetricsJobsError(
            f"manual metrics job '{name}' contains unsupported fields: {unsupported_fields}"
        )

    targets = _parse_targets(name, raw_job.get("targets"))
    labels = _parse_labels(name, raw_job.get("labels"), topology_labels=topology_labels)
    scrape_interval = _optional_duration_field(name, raw_job, "scrape_interval")
    scrape_timeout = _optional_duration_field(name, raw_job, "scrape_timeout")
    scheme = _optional_string_field(raw_job, "scheme", default="http").lower()
    if scheme not in {"http", "https"}:
        raise ManualMetricsJobsError(
            f"manual metrics job '{name}' field 'scheme' must be one of ['http', 'https']"
        )
    metrics_path = _optional_string_field(raw_job, "metrics_path", default="/metrics")
    insecure_skip_verify = _optional_bool_field(raw_job, "insecure_skip_verify", default=False)

    tls_config: dict[str, str | bool] = {}
    if insecure_skip_verify:
        tls_config["insecure_skip_verify"] = True

    return MetricsScrapeJob(
        job_name=name,
        targets=[
            ScrapeTarget(
                address=_normalize_scrape_target(target),
                labels=labels,
            )
            for target in targets
        ],
        metrics_path=metrics_path,
        scheme=scheme,
        scrape_interval=scrape_interval,
        scrape_timeout=scrape_timeout,
        tls_config=tls_config,
    )


def _parse_targets(name: str, raw_targets: object) -> list[str]:
    """Validate the targets field and return the normalized list."""
    if not isinstance(raw_targets, list) or not raw_targets:
        raise ManualMetricsJobsError(
            f"manual metrics job '{name}' field 'targets' must be a non-empty list"
        )

    targets: list[str] = []
    for index, target in enumerate(raw_targets):
        if not isinstance(target, str) or not target.strip():
            raise ManualMetricsJobsError(
                f"manual metrics job '{name}' target {index + 1} must be a non-empty string"
            )
        targets.append(target.strip())
    return targets


def _parse_labels(
    name: str,
    raw_labels: object,
    *,
    topology_labels: Mapping[str, str],
) -> dict[str, str]:
    """Validate operator labels and merge them with local topology labels."""
    if raw_labels in (None, {}):
        return dict(topology_labels)
    if not isinstance(raw_labels, Mapping):
        raise ManualMetricsJobsError(
            f"manual metrics job '{name}' field 'labels' must be a mapping"
        )

    labels: dict[str, str] = {}
    for key, value in raw_labels.items():
        if not isinstance(key, str) or not key.strip():
            raise ManualMetricsJobsError(
                f"manual metrics job '{name}' labels must use non-empty string keys"
            )
        if key.startswith(_RESERVED_LABEL_PREFIX):
            raise ManualMetricsJobsError(
                f"manual metrics job '{name}' labels must not override reserved juju labels"
            )
        if not isinstance(value, str) or not value:
            raise ManualMetricsJobsError(
                f"manual metrics job '{name}' label '{key}' must have a non-empty string value"
            )
        labels[key] = value
    return {**topology_labels, **labels}


def _required_string_field(raw_job: Mapping[str, object], field_name: str) -> str:
    """Return a required non-empty string field from a job mapping."""
    value = raw_job.get(field_name)
    if not isinstance(value, str) or not value.strip():
        raise ManualMetricsJobsError(
            f"manual metrics job field '{field_name}' must be a non-empty string"
        )
    return value.strip()


def _optional_string_field(
    raw_job: Mapping[str, object],
    field_name: str,
    *,
    default: str,
) -> str:
    """Return an optional string field, falling back to the provided default."""
    value = raw_job.get(field_name, default)
    if not isinstance(value, str):
        raise ManualMetricsJobsError(f"manual metrics job field '{field_name}' must be a string")
    value = value.strip()
    return value or default


def _optional_duration_field(
    name: str,
    raw_job: Mapping[str, object],
    field_name: str,
) -> str:
    """Return an optional duration field after validating the release-1 format."""
    if field_name not in raw_job or raw_job[field_name] in ("", None):
        return ""
    value = raw_job[field_name]
    if not isinstance(value, str) or not _DURATION_PATTERN.fullmatch(value.strip()):
        raise ManualMetricsJobsError(
            f"manual metrics job '{name}' field '{field_name}' must match '\\d+[ywdhms]'"
        )
    return value.strip()


def _optional_bool_field(
    raw_job: Mapping[str, object],
    field_name: str,
    *,
    default: bool,
) -> bool:
    """Return an optional bool field with strict type validation."""
    value = raw_job.get(field_name, default)
    if not isinstance(value, bool):
        raise ManualMetricsJobsError(f"manual metrics job field '{field_name}' must be a boolean")
    return value


def _normalize_scrape_target(target: str) -> str:
    """Normalize a scrape target, bracketing IPv6 host literals when needed."""
    target = target.strip()
    if not target or target.startswith("["):
        return target
    if target.count(":") <= 1:
        return target
    host, _, port = target.rpartition(":")
    if host and port.isdigit():
        try:
            resolved_host = socket.gethostbyaddr(host)[0]
        except (socket.herror, OSError):
            resolved_host = ""
        if resolved_host:
            return f"{resolved_host}:{port}"
        return f"[{host}]:{port}"
    return f"[{target}]"
