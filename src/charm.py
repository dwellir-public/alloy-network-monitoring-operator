#!/usr/bin/env python3
# Copyright 2025 Erik Lönroth
# See LICENSE file for licensing details.
"""Charm the application."""

import base64
import binascii
import json
import logging
import re
import shlex
import socket
import ssl
import subprocess
import tempfile
import zlib
from collections.abc import Mapping
from pathlib import Path
from typing import TypeAlias, cast

import ops
from charms.dwellir_observability.v0.machine_observability import (
    MAX_SERIALIZED_PAYLOAD_BYTES,
    MachineObservabilityPayload,
    PayloadTooLargeError,
)
from charms.grafana_cloud_integrator.v0.cloud_config_requirer import GrafanaCloudConfigRequirer
from charms.loki_k8s.v1.loki_push_api import LokiPushApiConsumer
from charms.prometheus_k8s.v0.prometheus_scrape import MetricsEndpointConsumer
from charms.prometheus_k8s.v1.prometheus_remote_write import PrometheusRemoteWriteConsumer
from cosl import JujuTopology
from pydantic import ValidationError
from pydantic_core import from_json

import alloy
from alert_rules import (
    CosToolRuleValidator,
    build_rule_state,
    publish_rule_groups,
    validate_rule_groups,
)
from config_builder import (
    DEFAULT_CONFIG_BACKUP_PATH,
    DEFAULT_CONFIG_PATH,
    DEFAULT_PACKAGE_CONFIG_BACKUP_PATH,
    ConfigBuilder,
    InvalidLogLevelError,
    LogSourceGroup,
    MetricsScrapeJob,
    ScrapeTarget,
)
from grafanacloud_connectivity import probe_endpoint
from machine_observability_sources import (
    MachineObservabilitySource,
    translate_machine_observability_payload,
)
from manual_metrics_jobs import ManualMetricsJobsError, parse_manual_metrics_jobs
from network_alert_settings import NetworkAlertsError, NetworkAlertSettings, parse_alert_settings
from network_alerts import device_alert_groups, network_alert_groups, network_log_alert_groups
from network_monitoring import (
    NetworkMonitoringError,
    NetworkPipeline,
    parse_location_labels,
    parse_netbox_token,
    parse_network_settings,
    parse_snmp_authentications,
    read_snmp_module_errors,
)
from network_syslog import SyslogParsersError
from outbound_endpoints import OutboundEndpoint, dedupe_endpoints
from secret_files import SecretFiles, remove_secret_files, stage_secret_files

logger = logging.getLogger(__name__)

SYSLOG_RELATION_NAME = "syslog-receiver"
METRICS_RELATION_NAME = "metrics-endpoint"
METRICS_JOB_NAME_FIELD = "metrics_job_name"
SYSLOG_RECEIVER_PORT = "1514"
SYSLOG_RECEIVER_PROTOCOLS = "tcp,udp"
SYSLOG_RECOMMENDED_PROTOCOL = "tcp"
REMOTE_WRITE_LEGACY_METADATA_KEYS = ("tenant-id", "application", "model", "model_uuid")
RULE_CACHE_KEY = "_alloy_vm_rule_state_v1"
RULE_CACHE_VALUE_LIMIT = 60 * 1024
RULE_CACHE_DECODED_LIMIT = 2 * 1024 * 1024
RULE_PUBLICATION_VALUE_LIMIT = 60 * 1024
_RULE_CACHE_READ_VALUE_LIMIT = 64 * 1024
_RULE_CACHE_READ_DECODED_LIMIT = 2 * 1024 * 1024
_RULE_CACHE_MAX_DEPTH = 64
_RULE_CACHE_MAX_CONTAINER_ITEMS = 10_000
_RULE_CACHE_MAX_NODES = 50_000
_RULE_CACHE_MAX_STRING_BYTES = 64 * 1024
_RULE_ARTIFACT_TYPES = {
    "prometheus_alert_rules": "prometheus",
    "loki_alert_rules": "loki",
}
_CACHE_ARTIFACT_ID_PATTERN = re.compile(r"^[a-z0-9][a-z0-9._-]{0,127}$")
RuleCacheState: TypeAlias = dict[str, dict[str, object]]


class PayloadDecodeError(ValueError):
    """Represent a safely categorized bounded JSON decoding failure."""


class NetworkSecretUnavailableError(NetworkMonitoringError):
    """Report an unavailable secret so existing network collection can be withdrawn."""


def parse_machine_observability_payload_json(raw_payload: str) -> object:
    """Parse raw relation JSON after enforcing the shared UTF-8 ceiling."""
    payload_bytes = len(raw_payload.encode("utf-8"))
    if payload_bytes > MAX_SERIALIZED_PAYLOAD_BYTES:
        raise PayloadTooLargeError(
            f"serialized machine-observability payload is {payload_bytes} bytes; "
            f"maximum is {MAX_SERIALIZED_PAYLOAD_BYTES} bytes"
        )
    try:
        return from_json(raw_payload)
    except ValueError as exc:
        raise PayloadDecodeError("json") from exc


def _valid_ownership_component(value: object) -> bool:
    """Accept a bounded printable cache ownership path component."""
    return (
        isinstance(value, str)
        and value == value.strip()
        and 0 < len(value.encode("utf-8")) <= 256
        and "/" not in value
        and value.isprintable()
    )


class _MachineObservabilityLogFilter(logging.Filter):
    """Redact canonical consumer validation details that may contain artifact input."""

    def filter(self, record: logging.LogRecord) -> bool:
        """Replace detailed validation output with its relation-scoped category."""
        if record.name == "charms.dwellir_observability.v0.machine_observability" and str(
            record.msg
        ).startswith("Invalid machine-observability payload on relation"):
            relation_id = (
                record.args[0] if isinstance(record.args, tuple) and record.args else "unknown"
            )
            record.msg = "Invalid machine-observability payload on relation %s: validation"
            record.args = (relation_id,)
        return True


class AlloyCharm(ops.CharmBase):
    """Machine charm for Grafana Alloy."""

    _stored = ops.StoredState()

    def __init__(self, *args):
        super().__init__(*args)
        consumer_logger = logging.getLogger(
            "charms.dwellir_observability.v0.machine_observability"
        )
        if not any(
            isinstance(item, _MachineObservabilityLogFilter) for item in consumer_logger.filters
        ):
            consumer_logger.addFilter(_MachineObservabilityLogFilter())
        self._stored.set_default(
            last_good_config="",
            last_failed_config_path="",
            config_drifted=False,
            last_custom_args="",
            last_live_debugging=False,
            last_syslog_receivers_enabled=False,
            livedebug_prev_custom_args="",
            livedebug_prev_custom_args_set=False,
            network_secret_files={},
            last_network_collector="",
            network_feature_config={},
        )
        self._network_pipeline: NetworkPipeline | None = None
        self._location_labels: dict[str, str] = {}
        self._network_features = NetworkAlertSettings()
        self._network_status: ops.StatusBase | None = None
        self._staged_secrets: SecretFiles | None = None
        self._charm_refresh = False
        self._refresh_status: ops.StatusBase | None = None
        # Observe before libraries: their upgrade events can emit endpoint events.
        self.framework.observe(self.on.upgrade_charm, self._on_upgrade_charm)
        self._topology = JujuTopology.from_charm(self)
        self._rule_validator = CosToolRuleValidator()
        self._loki_consumer = LokiPushApiConsumer(
            self,
            relation_name="send-loki-logs",
            forward_alert_rules=False,
            skip_alert_topology_labeling=True,
        )
        self._metrics_consumer = MetricsEndpointConsumer(
            self,
            relation_name="metrics-endpoint",
        )
        self.grafana_cloud = GrafanaCloudConfigRequirer(self)
        self._remote_write_consumer = PrometheusRemoteWriteConsumer(
            self,
            relation_name="send-remote-write",
            peer_relation_name="alloy-peers",
            forward_alert_rules=False,
        )
        self.framework.observe(self.on.install, self._on_install)
        self.framework.observe(self.on.start, self._on_start)
        self.framework.observe(self.on.config_changed, self._on_config_changed)
        self.framework.observe(self.on.update_status, self._on_update_status)
        self.framework.observe(self.on.secret_changed, self._on_network_secret_changed)
        self.framework.observe(
            self._loki_consumer.on.loki_push_api_endpoint_joined,
            self._on_loki_endpoint_changed,
        )
        self.framework.observe(
            self._loki_consumer.on.loki_push_api_endpoint_departed,
            self._on_loki_endpoint_changed,
        )
        self.framework.observe(
            self._metrics_consumer.on.targets_changed,
            self._on_metrics_relation_changed,
        )
        self.framework.observe(
            self._remote_write_consumer.on.endpoints_changed,
            self._on_metrics_relation_changed,
        )
        for event_name in (
            "relation_joined",
            "relation_changed",
            "relation_broken",
            "relation_departed",
        ):
            self.framework.observe(
                getattr(self.on["machine-observability"], event_name),
                self._on_machine_observability_relation_event,
            )
        for event in (
            self.on["grafana-cloud-config"].relation_joined,
            self.on["grafana-cloud-config"].relation_changed,
            self.on["grafana-cloud-config"].relation_broken,
        ):
            self.framework.observe(event, self._on_observability_endpoint_changed)
        self.framework.observe(
            self.on[SYSLOG_RELATION_NAME].relation_created,
            self._on_syslog_receiver_relation_event,
        )
        self.framework.observe(
            self.on[SYSLOG_RELATION_NAME].relation_broken,
            self._on_syslog_receiver_relation_event,
        )
        for relation_name in ("send-loki-logs", "send-remote-write"):
            for event_name in ("relation_joined", "relation_changed", "relation_broken"):
                self.framework.observe(
                    getattr(self.on[relation_name], event_name),
                    self._on_rule_relation_event,
                )
        self.framework.observe(self.on.leader_elected, self._on_rule_relation_event)
        self.framework.observe(self.on.upgrade_charm, self._on_rule_relation_event)

    def _on_install(self, event):
        self.unit.status = ops.MaintenanceStatus("Installing Alloy")
        try:
            alloy.install()
            alloy.preserve_default_config(
                config_path=Path(DEFAULT_CONFIG_PATH),
                preserved_path=Path(DEFAULT_PACKAGE_CONFIG_BACKUP_PATH),
            )
        except subprocess.CalledProcessError as exc:
            self.unit.status = ops.MaintenanceStatus(f"Installation failed: {exc}")
            event.defer()

    def _on_start(self, event):
        try:
            self.unit.status = ops.MaintenanceStatus("Starting Alloy")
            try:
                alloy.start()
            except subprocess.CalledProcessError as exc:
                self.unit.status = ops.MaintenanceStatus(f"Failed to start Alloy: {exc}")
                event.defer()
                return
            version = alloy.get_version()
            if version is not None:
                self.unit.set_workload_version(version)
            self._refresh_remote_write_relation_metadata()
            self._refresh_syslog_receiver_relations()
            if self.config.get("network-monitoring-enabled") or self._stored.network_secret_files:
                self.unit.status = self._configure() or ops.BlockedStatus(
                    "Alloy configuration failed"
                )
            else:
                self.unit.status = ops.ActiveStatus("Alloy is running")
        finally:
            self._reconcile_rule_groups(event)

    def _on_config_changed(self, event):
        try:
            self.unit.status = ops.MaintenanceStatus("Configuring Alloy")
            status = self._configure()
            if status is not None:
                self._refresh_remote_write_relation_metadata()
                self._refresh_syslog_receiver_relations()
                self.unit.status = status
            elif not isinstance(self.unit.status, ops.BlockedStatus):
                self.unit.status = ops.MaintenanceStatus(
                    "Invalid Alloy config. No changes applied."
                )
        finally:
            self._reconcile_rule_groups(event)

    def _on_upgrade_charm(self, event):
        """Handle charm upgrade without restarting or rewriting configuration."""
        self._charm_refresh = True
        self._refresh_status = self.unit.status
        self._refresh_remote_write_relation_metadata()
        logger.info("Upgrade-charm event: skipping config rewrite and restart.")

    def _on_loki_endpoint_changed(self, event):
        """Update config when Loki endpoints change."""
        self.unit.status = ops.MaintenanceStatus("Updating Loki endpoints")
        status = self._configure()
        if status is not None:
            self._refresh_syslog_receiver_relations()
            self.unit.status = status
        elif not isinstance(self.unit.status, ops.BlockedStatus):
            self.unit.status = ops.MaintenanceStatus("Invalid Alloy config. No changes applied.")

    def _on_metrics_relation_changed(self, event):
        """Update config when metrics scrape or remote write endpoints change."""
        self.unit.status = ops.MaintenanceStatus("Updating metrics endpoints")
        status = self._configure()
        if status is not None:
            self._refresh_remote_write_relation_metadata()
            self.unit.status = status
        elif not isinstance(self.unit.status, ops.BlockedStatus):
            self.unit.status = ops.MaintenanceStatus("Invalid Alloy config. No changes applied.")

    def _on_observability_endpoint_changed(self, event):
        """Update config when Grafana Cloud endpoints change."""
        self.unit.status = ops.MaintenanceStatus("Updating observability endpoints")
        status = self._configure()
        if status is not None:
            self._refresh_remote_write_relation_metadata()
            self._refresh_syslog_receiver_relations()
            self.unit.status = status
        elif not isinstance(self.unit.status, ops.BlockedStatus):
            self.unit.status = ops.MaintenanceStatus("Invalid Alloy config. No changes applied.")

    def _on_machine_observability_relation_event(self, event: ops.RelationEvent) -> None:
        """Reconcile config and rules under one relation event owner."""
        try:
            self._on_observability_endpoint_changed(event)
        finally:
            self._on_rule_relation_event(event)

    def _on_update_status(self, event):
        """Handle periodic status updates (detect drift and workload health)."""
        try:
            if self.config.get("network-monitoring-enabled") or self._stored.network_secret_files:
                status = self._configure(reconcile_drift=False)
                if status is not None:
                    self.unit.status = status
                if isinstance(status, (ops.BlockedStatus, ops.WaitingStatus)):
                    return
            if not alloy.is_active():
                self.unit.status = ops.MaintenanceStatus("Alloy service not running")
                self._refresh_syslog_receiver_relations()
                return
            version = alloy.get_version()
            if version is not None:
                self.unit.set_workload_version(version)
            self._reconcile_config_drift_status()
            self._refresh_syslog_receiver_relations()
            if self._stored.config_drifted:
                return
            module_status = self._network_module_status()
            if module_status is not None:
                self.unit.status = module_status
                return
            connectivity_error = self._grafana_cloud_status_error()
            if connectivity_error is not None:
                self.unit.status = ops.BlockedStatus(connectivity_error)
                return
            if self._is_grafana_cloud_status() or self._is_service_down_status():
                self.unit.status = self._post_config_status("Alloy is running.")
        finally:
            self._reconcile_rule_groups(event)

    def _network_module_status(self) -> ops.StatusBase | None:
        """Report discovered module errors while valid targets continue collecting."""
        if not self._network_pipeline:
            return None
        invalid_devices = read_snmp_module_errors(self._self_metrics_address())
        if invalid_devices is None:
            return ops.WaitingStatus(
                "Unable to verify NetBox snmp_modules through the local Alloy API"
            )
        if not invalid_devices:
            return None
        device_ids = ", ".join(invalid_devices[:5])
        remaining = len(invalid_devices) - 5
        suffix = f" (+{remaining} more)" if remaining > 0 else ""
        return ops.BlockedStatus(
            f"Invalid NetBox snmp_modules for device IDs {device_ids}{suffix}; "
            "use supported module names without spaces"
        )

    def _on_syslog_receiver_relation_event(self, event):
        """Publish current syslog receiver details for related requirers."""
        self._refresh_syslog_receiver_relations()

    def _refresh_remote_write_relation_metadata(self) -> None:
        """Remove legacy app-level metadata from remote-write relations."""
        if not self.unit.is_leader():
            return
        for relation in self.model.relations.get("send-remote-write", []):
            for key in REMOTE_WRITE_LEGACY_METADATA_KEYS:
                relation.data[self.app].pop(key, None)

    def _on_rule_relation_event(self, event: ops.EventBase) -> None:
        """Reconcile rule publication after relation and leadership events."""
        self._refresh_remote_write_relation_metadata()
        self._reconcile_rule_groups(event)

    def _reconcile_rule_groups(self, event: ops.EventBase) -> None:
        """Publish transformed principal rules as complete standard-relation state."""
        if not self.unit.is_leader():
            return
        self._rule_validator.begin_reconcile()

        removed_relation_id = None
        event_relation = getattr(event, "relation", None)
        if (
            isinstance(event, ops.RelationBrokenEvent)
            and event_relation is not None
            and event_relation.name == "machine-observability"
        ):
            removed_relation_id = str(event_relation.id)

        relations = {
            relation.id: relation
            for relation in self.model.relations.get("machine-observability", [])
            if str(relation.id) != removed_relation_id
        }
        previous_states = {
            relation_id: self._load_relation_rule_cache(relation)
            for relation_id, relation in sorted(relations.items())
        }
        relation_state = self._bounded_existing_rule_state(previous_states)
        for relation_id, relation in sorted(relations.items()):
            desired = self._desired_relation_rule_state(relation, previous_states[relation_id])
            if desired is None:
                continue
            desired = self._without_ownership_conflicts(
                desired,
                {
                    other_id: state
                    for other_id, state in relation_state.items()
                    if other_id < relation_id
                },
                relation_id=relation_id,
                fallback=previous_states[relation_id],
            )
            candidate = self._normalize_ownership_state({**relation_state, relation_id: desired})
            if not self._rule_state_is_publishable(candidate):
                logger.warning(
                    "Machine-observability rules on relation %s rejected: publish-size",
                    relation_id,
                )
                continue
            if self._store_relation_rule_cache(relation, desired):
                relation_state = candidate

        prometheus_groups, loki_groups = self._flatten_rule_state(relation_state)
        prometheus_groups = self._with_network_rule_groups(
            prometheus_groups, self._network_alert_groups(), "metric"
        )
        loki_groups = self._with_network_rule_groups(
            loki_groups, self._network_log_alert_groups(), "log"
        )
        publish_rule_groups(self, "send-remote-write", prometheus_groups)
        publish_rule_groups(self, "send-loki-logs", loki_groups)
        self._set_rule_destination_status(prometheus_groups, loki_groups)

    def _with_network_rule_groups(self, related, owned, kind):
        """Keep related rules when owned additions exceed the existing transport limit."""
        combined = related + owned
        payload = json.dumps({"groups": combined}, sort_keys=True, separators=(",", ":"))
        if len(payload.encode()) < RULE_PUBLICATION_VALUE_LIMIT:
            return combined
        if owned:
            self.unit.status = ops.BlockedStatus(
                f"Network {kind} alert rules exceed relation capacity"
            )
        return related

    def _validate_network_rule_capacity(self) -> None:
        """Reject oversized candidate presets before replacing applied settings or rules."""
        if str(self.config.get("config-override", "")).strip():
            return
        topology = self._network_topology_labels()
        owner = f"{self.model.uuid}_{self.app.name}"
        collector = (
            self._network_pipeline.settings.collector
            if self._network_pipeline
            else self._stored.last_network_collector
        )
        metrics = []
        if (
            self.config.get("network-monitoring-enabled")
            and collector
            and self._network_features.metrics_enabled
        ):
            metrics = network_alert_groups(collector, owner, topology_labels=topology)
            metrics += device_alert_groups(
                collector, owner, topology_labels=topology, settings=self._network_features
            )
        logs = (
            network_log_alert_groups(
                owner, topology_labels=topology, settings=self._network_features
            )
            if self.config.get("enable-syslogreceivers")
            else []
        )
        related_state = (
            {
                relation.id: self._load_relation_rule_cache(relation)
                for relation in self.model.relations.get("machine-observability", [])
            }
            if self.unit.is_leader()
            else {}
        )
        related_metrics, related_logs = self._flatten_rule_state(
            self._bounded_existing_rule_state(related_state)
        )
        for kind, related, owned in (
            ("metric", related_metrics, metrics),
            ("log", related_logs, logs),
        ):
            payload = json.dumps(
                {"groups": related + owned}, sort_keys=True, separators=(",", ":")
            )
            if owned and len(payload.encode()) >= RULE_PUBLICATION_VALUE_LIMIT:
                raise NetworkAlertsError(
                    f"Network {kind} alert rules exceed relation capacity; "
                    "reduce scopes or presets"
                )

    def _network_alert_groups(self) -> list[dict[str, object]]:
        """Publish owned network rules while preserving existing relation contracts."""
        applied = dict(self._stored.network_feature_config)
        if not applied.get(
            "network-monitoring-enabled", self.config.get("network-monitoring-enabled")
        ) or applied.get(
            "override-active", bool(str(self.config.get("config-override", "")).strip())
        ):
            return []
        features = parse_alert_settings(applied) if applied else NetworkAlertSettings()
        if not features.metrics_enabled:
            return []
        collector = self._stored.last_network_collector
        if not collector:
            return []
        owner = f"{self.model.uuid}_{self.app.name}"
        topology = self._network_topology_labels()
        return network_alert_groups(
            collector,
            owner,
            topology_labels=topology,
        ) + device_alert_groups(collector, owner, topology_labels=topology, settings=features)

    def _network_topology_labels(self) -> dict[str, str]:
        """Return collector provenance for owned rules and generated telemetry."""
        return self._topology.as_dict(
            remapped_keys={
                "model": "juju_model",
                "model_uuid": "juju_model_uuid",
                "application": "juju_application",
                "unit": "juju_unit",
                "charm_name": "juju_charm",
            }
        )

    def _network_log_alert_groups(self) -> list[dict[str, object]]:
        """Publish only log rules for the successfully applied receiver/parser settings."""
        applied = dict(self._stored.network_feature_config)
        if (
            not applied
            or not applied.get("enable-syslogreceivers")
            or applied.get("override-active")
        ):
            return []
        return network_log_alert_groups(
            f"{self.model.uuid}_{self.app.name}",
            topology_labels=self._network_topology_labels(),
            settings=parse_alert_settings(applied),
        )

    def _commit_network_features(self) -> None:
        """Remember validated public controls only after their runtime generation applies."""
        names = (
            "network-alerts-enabled",
            "network-device-alerts-enabled",
            "network-log-alerts-enabled",
            "network-alerts-config",
            "syslog-parsers",
            "network-monitoring-enabled",
            "enable-syslogreceivers",
        )
        defaults = {
            "network-alerts-enabled": True,
            "network-device-alerts-enabled": False,
            "network-log-alerts-enabled": False,
            "network-alerts-config": "",
            "syslog-parsers": "",
            "network-monitoring-enabled": False,
            "enable-syslogreceivers": False,
        }
        self._stored.network_feature_config = {
            **{name: self.config.get(name, defaults[name]) for name in names},
            "override-active": bool(str(self.config.get("config-override", "")).strip()),
        }
        if self._stored.network_feature_config["override-active"]:
            self._stored.network_feature_config["network-alerts-config"] = ""
            self._stored.network_feature_config["syslog-parsers"] = ""

    def _set_rule_destination_status(
        self,
        prometheus_groups: list[dict[str, object]],
        loki_groups: list[dict[str, object]],
    ) -> None:
        """Report accepted rule state waiting for its standard backend relation."""
        missing: list[str] = []
        if prometheus_groups and not self.model.relations.get("send-remote-write", []):
            missing.append("send-remote-write relation")
        if loki_groups and not self.model.relations.get("send-loki-logs", []):
            missing.append("send-loki-logs relation")
        if missing and isinstance(self.unit.status, ops.ActiveStatus):
            self.unit.status = ops.WaitingStatus(
                f"Waiting for {' and '.join(missing)} for accepted alert rules"
            )

    def _desired_relation_rule_state(
        self,
        relation: ops.Relation,
        previous: RuleCacheState,
    ) -> RuleCacheState | None:
        """Parse current relation input and apply it to the relation's durable LKG."""
        raw_payload = relation.data[relation.app].get("payload", "{}") if relation.app else "{}"
        try:
            payload = parse_machine_observability_payload_json(raw_payload)
        except (PayloadDecodeError, PayloadTooLargeError) as exc:
            category = "size" if isinstance(exc, PayloadTooLargeError) else "json"
            logger.warning(
                "Invalid machine-observability rule payload on relation %s: %s",
                relation.id,
                category,
            )
            return None
        return self._build_relation_rule_state(payload, previous, str(relation.id))

    def _bounded_existing_rule_state(
        self,
        previous_states: dict[int, RuleCacheState],
    ) -> dict[int, RuleCacheState]:
        """Admit cached relation states deterministically without exceeding publication limits."""
        admitted: dict[int, RuleCacheState] = {}
        for relation_id, state in sorted(previous_states.items()):
            state = self._without_ownership_conflicts(state, admitted, relation_id=relation_id)
            candidate = {**admitted, relation_id: state}
            if self._rule_state_is_publishable(candidate):
                admitted = candidate
            else:
                logger.warning(
                    "Machine-observability cached rules on relation %s skipped: publish-size",
                    relation_id,
                )
        return admitted

    @staticmethod
    def _normalize_ownership_state(states: dict[int, RuleCacheState]) -> dict[int, RuleCacheState]:
        """Resolve all cross-relation ownership collisions by ascending relation id."""
        admitted: dict[int, RuleCacheState] = {}
        for relation_id, state in sorted(states.items()):
            admitted[relation_id] = AlloyCharm._without_ownership_conflicts(
                state,
                admitted,
                relation_id=relation_id,
            )
        return admitted

    @staticmethod
    def _without_ownership_conflicts(
        state: RuleCacheState,
        admitted: dict[int, RuleCacheState],
        *,
        relation_id: int,
        fallback: RuleCacheState | None = None,
    ) -> RuleCacheState:
        """Resolve later collisions, retaining a non-conflicting prior artifact when possible."""
        claimed = {
            cast(str, entry["ownership"])
            for other_relation, other_state in admitted.items()
            if other_relation != relation_id
            for entry in other_state.values()
        }
        result: RuleCacheState = {}
        for identity, entry in state.items():
            ownership = cast(str, entry["ownership"])
            if ownership in claimed:
                logger.warning(
                    "Machine-observability rules on relation %s rejected: duplicate-ownership %s",
                    relation_id,
                    ownership,
                )
                prior = fallback.get(identity) if fallback is not None else None
                if prior is not None:
                    prior_ownership = cast(str, prior["ownership"])
                    if prior_ownership not in claimed:
                        claimed.add(prior_ownership)
                        result[identity] = prior
                continue
            claimed.add(ownership)
            result[identity] = entry
        return result

    @staticmethod
    def _flatten_rule_state(
        relation_state: dict[int, RuleCacheState],
    ) -> tuple[list[dict[str, object]], list[dict[str, object]]]:
        """Flatten relation-partitioned ownership state into deterministic backend groups."""
        prometheus: dict[str, list[dict[str, object]]] = {}
        loki: dict[str, list[dict[str, object]]] = {}
        for relation_id in sorted(relation_state):
            state = relation_state[relation_id]
            for entry in state.values():
                target = prometheus if entry["backend"] == "prometheus" else loki
                ownership = cast(str, entry["ownership"])
                if ownership in target:
                    raise ValueError("duplicate-ownership")
                target[ownership] = cast(list[dict[str, object]], entry["groups"])
        prometheus_groups = [
            group for ownership in sorted(prometheus) for group in prometheus[ownership]
        ]
        loki_groups = [group for ownership in sorted(loki) for group in loki[ownership]]
        for groups in (prometheus_groups, loki_groups):
            names = [cast(str, group["name"]) for group in groups]
            if len(names) != len(set(names)):
                raise ValueError("duplicate-group")
        return prometheus_groups, loki_groups

    @staticmethod
    def _rule_state_is_publishable(relation_state: dict[int, RuleCacheState]) -> bool:
        """Check the exact compact downstream values against a conservative Juju ceiling."""
        try:
            flattened = AlloyCharm._flatten_rule_state(relation_state)
        except (KeyError, TypeError, ValueError):
            return False
        for groups in flattened:
            payload = json.dumps({"groups": groups}, sort_keys=True, separators=(",", ":"))
            if len(payload.encode("utf-8")) > RULE_PUBLICATION_VALUE_LIMIT:
                return False
        return True

    def _load_relation_rule_cache(self, relation: ops.Relation) -> RuleCacheState:
        """Load and validate transformed LKG state from this relation's app databag."""
        raw_cache = relation.data[self.app].get(RULE_CACHE_KEY)
        if not raw_cache:
            return {}
        try:
            state = self._decode_rule_cache(raw_cache)
            if not self._valid_rule_cache_state(state):
                raise ValueError("schema")
        except (
            ValueError,
            TypeError,
            UnicodeDecodeError,
            binascii.Error,
            zlib.error,
            json.JSONDecodeError,
            RecursionError,
        ):
            logger.warning(
                "Invalid machine-observability rule cache on relation %s: cache-validation",
                relation.id,
            )
            return {}
        return cast(RuleCacheState, state)

    def _store_relation_rule_cache(self, relation: ops.Relation, state: RuleCacheState) -> bool:
        """Persist transformed LKG state before publication, retaining prior state on failure."""
        content = self._serialize_rule_cache(state)
        encoded = self._encode_rule_cache_content(content)
        if (
            len(content) > RULE_CACHE_DECODED_LIMIT
            or len(encoded.encode("utf-8")) > RULE_CACHE_VALUE_LIMIT
        ):
            logger.warning(
                "Machine-observability rule cache on relation %s rejected: cache-size",
                relation.id,
            )
            return False
        if relation.data[self.app].get(RULE_CACHE_KEY) == encoded:
            return True
        try:
            relation.data[self.app][RULE_CACHE_KEY] = encoded
        except (ops.ModelError, RuntimeError):
            logger.warning(
                "Machine-observability rule cache on relation %s rejected: cache-write",
                relation.id,
            )
            return False
        return True

    @staticmethod
    def _encode_rule_cache(state: RuleCacheState) -> str:
        """Encode transformed state as deterministic compact compressed JSON."""
        content = AlloyCharm._serialize_rule_cache(state)
        return AlloyCharm._encode_rule_cache_content(content)

    @staticmethod
    def _serialize_rule_cache(state: RuleCacheState) -> bytes:
        """Serialize transformed state as deterministic compact JSON bytes."""
        return json.dumps(state, sort_keys=True, separators=(",", ":")).encode("utf-8")

    @staticmethod
    def _encode_rule_cache_content(content: bytes) -> str:
        """Compress and base64-encode serialized cache bytes with a version prefix."""
        return "v1:" + base64.b64encode(zlib.compress(content, level=9)).decode("ascii")

    @staticmethod
    def _decode_rule_cache(raw_cache: str) -> object:
        """Decode a versioned cache with a strict decompressed-size ceiling."""
        if (
            not raw_cache.startswith("v1:")
            or len(raw_cache.encode("utf-8")) > _RULE_CACHE_READ_VALUE_LIMIT
        ):
            raise ValueError("format")
        compressed = base64.b64decode(raw_cache[3:], validate=True)
        decoder = zlib.decompressobj()
        content = decoder.decompress(compressed, _RULE_CACHE_READ_DECODED_LIMIT + 1)
        if len(content) > _RULE_CACHE_READ_DECODED_LIMIT or not decoder.eof or decoder.unused_data:
            raise ValueError("size")
        parsed = json.loads(content)
        if not AlloyCharm._cache_structure_is_bounded(parsed):
            raise ValueError("structure")
        return parsed

    @staticmethod
    def _cache_structure_is_bounded(value: object) -> bool:
        """Bound cache nesting, fan-out, nodes, and strings using an iterative walk."""
        nodes = 0
        stack: list[tuple[object, int]] = [(value, 0)]
        while stack:
            item, depth = stack.pop()
            nodes += 1
            if nodes > _RULE_CACHE_MAX_NODES or depth > _RULE_CACHE_MAX_DEPTH:
                return False
            if isinstance(item, str):
                if len(item.encode("utf-8")) > _RULE_CACHE_MAX_STRING_BYTES:
                    return False
            elif isinstance(item, dict):
                if len(item) > _RULE_CACHE_MAX_CONTAINER_ITEMS:
                    return False
                stack.extend((key, depth + 1) for key in item)
                stack.extend((child, depth + 1) for child in item.values())
            elif isinstance(item, list):
                if len(item) > _RULE_CACHE_MAX_CONTAINER_ITEMS:
                    return False
                stack.extend((child, depth + 1) for child in item)
        return True

    def _valid_rule_cache_state(self, state: object) -> bool:
        """Accept only canonical transformed ownership entries from the private cache."""
        if not isinstance(state, dict):
            return False
        for identity, entry in state.items():
            if (
                not isinstance(identity, str)
                or identity.count("/") != 1
                or not isinstance(entry, dict)
            ):
                return False
            artifact_type, artifact_id = identity.split("/", 1)
            expected_backend = _RULE_ARTIFACT_TYPES.get(artifact_type)
            if (
                not expected_backend
                or _CACHE_ARTIFACT_ID_PATTERN.fullmatch(artifact_id) is None
                or set(entry) != {"backend", "ownership", "groups"}
            ):
                return False
            ownership = entry.get("ownership")
            if not isinstance(ownership, str) or ownership.count("/") != 3:
                return False
            model_uuid, application, owned_type, owned_id = ownership.split("/", 3)
            if (
                not _valid_ownership_component(model_uuid)
                or not _valid_ownership_component(application)
                or (owned_type, owned_id) != (artifact_type, artifact_id)
            ):
                return False
            groups = entry.get("groups")
            if entry.get("backend") != expected_backend or not validate_rule_groups(groups):
                return False
        return True

    def _build_relation_rule_state(
        self,
        payload: object,
        previous: RuleCacheState,
        relation_id: str,
    ) -> RuleCacheState | None:
        """Apply one structurally valid relation snapshot to its per-artifact LKG."""
        snapshot = self._validated_rule_snapshot(payload, relation_id)
        if snapshot is None:
            return None
        schema_version, identities, identities_complete = snapshot
        if schema_version in (1, 2):
            return {}

        result = build_rule_state(payload, validator=self._validate_artifact_rules)
        for error in result.errors:
            logger.warning("Invalid machine-observability artifact: %s", error)
        desired = (
            dict(previous)
            if not identities_complete
            else {identity: previous[identity] for identity in identities if identity in previous}
        )
        for backend, ownership_state in (
            ("prometheus", result.prometheus),
            ("loki", result.loki),
        ):
            for ownership, groups in ownership_state.items():
                artifact_type, artifact_id = ownership.rsplit("/", 2)[-2:]
                desired[f"{artifact_type}/{artifact_id}"] = {
                    "backend": backend,
                    "ownership": ownership,
                    "groups": groups,
                }
        return desired

    def _validate_artifact_rules(
        self, artifact_type: str, groups: list[dict[str, object]]
    ) -> bool:
        """Validate one transformed artifact with the packaged backend parser."""
        return self._rule_validator(artifact_type, groups)

    @staticmethod
    def _validated_rule_snapshot(
        payload: object,
        relation_id: str,
    ) -> tuple[int, list[str], bool] | None:
        """Validate outer payload structure without rejecting individual artifacts."""
        header = AlloyCharm._validated_rule_snapshot_header(payload, relation_id)
        if header is None:
            return None
        schema_version, artifacts = header
        identities: list[str] = []
        identities_complete = True
        for artifact in artifacts:
            if not isinstance(artifact, dict):
                logger.warning(
                    "Invalid machine-observability rule payload on relation %s: schema",
                    relation_id,
                )
                identities_complete = False
                continue
            artifact_type = artifact.get("artifact_type")
            artifact_id = artifact.get("artifact_id")
            if (
                not isinstance(artifact_type, str)
                or artifact_type not in _RULE_ARTIFACT_TYPES
                or not isinstance(artifact_id, str)
                or _CACHE_ARTIFACT_ID_PATTERN.fullmatch(artifact_id) is None
            ):
                logger.warning(
                    "Invalid machine-observability rule payload on relation %s: identity",
                    relation_id,
                )
                identities_complete = False
                continue
            identities.append(f"{artifact_type}/{artifact_id}")
        if len(identities) != len(set(identities)):
            logger.warning(
                "Invalid machine-observability rule payload on relation %s: duplicate-identity",
                relation_id,
            )
            return None
        return schema_version, identities, identities_complete

    @staticmethod
    def _validated_rule_snapshot_header(
        payload: object,
        relation_id: str,
    ) -> tuple[int, list[object]] | None:
        """Validate relation-level schema, version, artifact collection, and topology."""
        if not isinstance(payload, dict):
            logger.warning(
                "Invalid machine-observability rule payload on relation %s: schema",
                relation_id,
            )
            return None
        schema_version = payload.get("schema_version", 1)
        if schema_version not in (1, 2, 3):
            logger.warning(
                "Invalid machine-observability rule payload on relation %s: schema-version",
                relation_id,
            )
            return None
        artifacts = payload.get("artifacts", [])
        if not isinstance(artifacts, list):
            logger.warning(
                "Invalid machine-observability rule payload on relation %s: schema",
                relation_id,
            )
            return None
        if schema_version in (1, 2) and artifacts:
            logger.warning(
                "Invalid machine-observability rule payload on relation %s: schema",
                relation_id,
            )
            return None
        if schema_version == 3 and artifacts:
            topology = payload.get("source_topology")
            if not isinstance(topology, dict) or any(
                not _valid_ownership_component(topology.get(field))
                for field in ("model_uuid", "application")
            ):
                logger.warning(
                    "Invalid machine-observability rule payload on relation %s: topology",
                    relation_id,
                )
                return None
        base_payload = {**payload, "artifacts": []}
        try:
            MachineObservabilityPayload.model_validate(base_payload)
        except Exception:  # noqa: BLE001 - only a safe category is reported
            logger.warning(
                "Invalid machine-observability rule payload on relation %s: schema",
                relation_id,
            )
            return None

        return schema_version, artifacts

    def _network_secret_content(self, reference: str, option: str) -> dict[str, str]:
        """Retrieve and track the latest granted revision, with sanitized errors."""
        if not reference:
            raise NetworkSecretUnavailableError(
                f"{option} requires a granted Juju secret reference"
            )
        try:
            return self.model.get_secret(id=reference).get_content(refresh=True)
        except (ops.ModelError, ValueError):
            raise NetworkSecretUnavailableError(
                f"{option} secret is unavailable; check its reference and grant"
            ) from None

    def _prepare_network_pipeline(self) -> None:
        """Stage credentials only when collection has a destination and valid settings."""
        self._network_pipeline = None
        self._network_status = None
        settings = parse_network_settings(self.config)
        if settings is None:
            return
        if self.app.planned_units() != 1:
            self._network_status = ops.BlockedStatus(
                "Network monitoring requires one active unit per collector application"
            )
            return
        if not self._remote_write_endpoint_urls():
            self._network_status = ops.WaitingStatus(
                "Waiting for remote write before enabling network monitoring"
            )
            return
        try:
            token = parse_netbox_token(
                self._network_secret_content(settings.token_secret, "netbox-token")
            )
            auths = parse_snmp_authentications(
                self._network_secret_content(settings.snmp_secret, "snmp-auth")
            )
            contents = {"token": token, "snmp": auths.yaml_text}
            if settings.ca_secret:
                ca = self._network_secret_content(settings.ca_secret, "netbox-ca").get(
                    "ca-pem", ""
                )
                try:
                    ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT).load_verify_locations(cadata=ca)
                except (ssl.SSLError, ValueError, TypeError):
                    raise NetworkMonitoringError(
                        "netbox-ca secret must contain a valid ca-pem certificate bundle"
                    ) from None
                contents["ca"] = ca
        except NetworkSecretUnavailableError as exc:
            # Apply a configuration without network components before removing old credentials.
            self._network_status = ops.BlockedStatus(str(exc))
            return
        self._staged_secrets = stage_secret_files(
            contents, dict(self._stored.network_secret_files)
        )
        files = self._staged_secrets.paths
        self._network_pipeline = NetworkPipeline(
            settings, auths.names, files["token"], files["snmp"], files.get("ca", "")
        )

    def _on_network_secret_changed(self, event) -> None:
        """Reconcile granted credentials without restarting an unchanged configuration."""
        if self.config.get("network-monitoring-enabled") or self._stored.network_secret_files:
            status = self._configure()
            if status is not None:
                self.unit.status = status

    def _discard_staged_secrets(self) -> None:
        """Remove an unapplied credential generation, keeping the active generation."""
        if self._staged_secrets and self._staged_secrets.created:
            remove_secret_files(self._staged_secrets.paths)

    def _stop_network_on_access_failure(self) -> None:
        """Fail closed when revoked credentials cannot be withdrawn by configuration."""
        if (
            isinstance(self._network_status, ops.BlockedStatus)
            and self._stored.network_secret_files
        ):
            alloy.stop()

    def _commit_network_secrets(self) -> None:
        """Retire previous credentials only after the replacement is running."""
        previous = dict(self._stored.network_secret_files)
        current = self._staged_secrets.paths if self._staged_secrets else {}
        if self.config.get("config-override") and any(
            json.dumps(path) in self._stored.last_good_config for path in previous.values()
        ):
            return
        self._stored.network_secret_files = current
        if self._network_pipeline:
            self._stored.last_network_collector = self._network_pipeline.settings.collector
        if previous != current:
            remove_secret_files(previous)

    def _configure(self, *, reconcile_drift: bool = True) -> ops.StatusBase | None:
        """Validate and apply a complete generation before advancing applied state."""
        if self._charm_refresh:
            return self._refresh_status
        self._staged_secrets = None
        try:
            alloy.ensure_config_dir_permissions(str(Path(DEFAULT_CONFIG_PATH).parent))
            self._prepare_network_pipeline()
            self._location_labels = (
                {}
                if str(self.config.get("config-override", "")).strip()
                else parse_location_labels(self.config)
            )
            self._network_features = (
                NetworkAlertSettings()
                if str(self.config.get("config-override", "")).strip()
                else parse_alert_settings(self.config)
            )
            self._validate_network_rule_capacity()
            manual_metrics_jobs = self._manual_metrics_scrape_jobs()
        except (
            ManualMetricsJobsError,
            NetworkMonitoringError,
            NetworkAlertsError,
            SyslogParsersError,
        ) as exc:
            self._discard_staged_secrets()
            self._stop_network_on_access_failure()
            self.unit.status = ops.BlockedStatus(str(exc))
            return self.unit.status
        except OSError:
            self._discard_staged_secrets()
            return ops.BlockedStatus("Unable to stage protected network credentials")
        contract_error = self._machine_observability_contract_error()
        if contract_error is not None:
            self._discard_staged_secrets()
            self._stop_network_on_access_failure()
            return ops.BlockedStatus(contract_error)
        try:
            config_text = self._render_config_text(manual_metrics_jobs=manual_metrics_jobs)
        except ValueError as exc:
            self._discard_staged_secrets()
            self._stop_network_on_access_failure()
            message = (
                str(exc)
                if isinstance(exc, InvalidLogLevelError)
                else "Invalid Alloy runtime arguments"
            )
            return ops.BlockedStatus(message)
        desired_custom_args = self._desired_custom_args()
        live_debugging = self._live_debugging_enabled()
        syslog_receivers_enabled = self._syslog_receivers_enabled()
        restart_required = (
            desired_custom_args != self._stored.last_custom_args
            or live_debugging != self._stored.last_live_debugging
        )
        if (
            self._stored.last_good_config
            and config_text == self._stored.last_good_config
            and (not self._stored.config_drifted or not reconcile_drift)
            and not restart_required
            and syslog_receivers_enabled == self._stored.last_syslog_receivers_enabled
        ):
            logger.debug("Alloy config unchanged; skipping apply.")
            self._commit_network_features()
            return self._post_config_status(
                "Alloy config updated and valid", manual_metrics_jobs=manual_metrics_jobs
            )
        if not self._validate_config_text(config_text):
            self._discard_staged_secrets()
            self._stop_network_on_access_failure()
            failed_path = self._persist_failed_config(config_text)
            if failed_path:
                self._stored.last_failed_config_path = str(failed_path)
            return ops.BlockedStatus(
                "Alloy configuration validation failed; previous generation retained"
            )
        if not self._apply_config_generation(config_text, desired_custom_args, restart_required):
            self._discard_staged_secrets()
            return ops.BlockedStatus(
                "Alloy configuration apply failed; previous generation retained"
            )
        self._stored.last_good_config = config_text
        self._stored.config_drifted = False
        self._stored.last_custom_args = desired_custom_args
        self._stored.last_live_debugging = live_debugging
        self._stored.last_syslog_receivers_enabled = syslog_receivers_enabled
        self._commit_network_secrets()
        self._commit_network_features()
        return self._post_config_status(
            "Alloy config updated and valid", manual_metrics_jobs=manual_metrics_jobs
        )

    def _apply_config_generation(
        self, config_text: str, custom_args: str, restart_required: bool
    ) -> bool:
        """Restore configuration and runtime settings when a service apply fails."""
        config_path = Path(DEFAULT_CONFIG_PATH)
        defaults_path = Path(alloy.DEFAULT_SYSTEMD_DEFAULTS_PATH)
        previous_config = self._read_config_from_disk()
        try:
            previous_defaults = defaults_path.read_text() if defaults_path.exists() else None
        except OSError:
            return False
        previous_debug = self._stored.livedebug_prev_custom_args
        previous_debug_set = self._stored.livedebug_prev_custom_args_set
        api_address = self._network_api_address()
        try:
            alloy.write_config_text(
                config_text, config_path=config_path, backup_path=Path(DEFAULT_CONFIG_BACKUP_PATH)
            )
            self._write_alloy_systemd_unit_defaults()
            self._apply_live_debugging(baseline_custom_args=custom_args)
            if alloy.is_active() and not restart_required:
                self._reload_alloy(api_address)
            else:
                if restart_required:
                    logger.info("Alloy runtime arguments changed; restarting to apply settings")
                    self.unit.status = ops.MaintenanceStatus(
                        "Applying changed Alloy runtime arguments; restart required"
                    )
                alloy.restart()
                if api_address:
                    alloy.wait_ready(api_address)
        except (subprocess.SubprocessError, OSError, RuntimeError):
            logger.warning("Failed to apply Alloy generation; restoring previous files")
            self._stored.livedebug_prev_custom_args = previous_debug
            self._stored.livedebug_prev_custom_args_set = previous_debug_set
            try:
                alloy.restore_file(config_path, previous_config)
                alloy.restore_file(defaults_path, previous_defaults)
                if previous_config is not None:
                    if restart_required or not alloy.is_active():
                        alloy.restart()
                    else:
                        self._reload_alloy(api_address)
                else:
                    alloy.stop()
            except (subprocess.SubprocessError, OSError, RuntimeError):
                logger.warning("Unable to restore running Alloy generation")
                alloy.stop()
            self._stop_network_on_access_failure()
            return False
        return True

    def _network_api_address(self) -> str:
        """Return the managed listener used to confirm network configuration applies."""
        if not str(self.config.get("config-override", "")).strip() and (
            self._network_pipeline or self._stored.network_secret_files
        ):
            return self._self_metrics_address()
        return ""

    @staticmethod
    def _reload_alloy(api_address: str) -> None:
        """Keep existing service reload behavior outside the managed network path."""
        if api_address:
            alloy.reload(api_address=api_address)
        else:
            alloy.reload()

    def _reconcile_config_drift_status(self) -> None:
        """Detect config drift and update unit status if appropriate."""
        if not self._stored.last_good_config:
            if self._seed_last_good_config_from_disk():
                return
        drifted = self._has_config_drift()
        if drifted != self._stored.config_drifted:
            if drifted:
                logger.warning("Detected manual Alloy config change at %s", DEFAULT_CONFIG_PATH)
            else:
                logger.info("Alloy config drift cleared.")
        self._stored.config_drifted = drifted
        if drifted:
            if isinstance(self.unit.status, (ops.ActiveStatus, ops.MaintenanceStatus)):
                self.unit.status = ops.MaintenanceStatus(self._config_drift_message())
            return
        if self._is_drift_status():
            self.unit.status = ops.ActiveStatus()

    def _seed_last_good_config_from_disk(self) -> bool:
        """Populate last_good_config from disk when it is still unset."""
        on_disk = self._read_config_from_disk()
        if on_disk is None:
            return False
        self._stored.last_good_config = on_disk
        self._stored.config_drifted = False
        logger.debug(
            "Seeded last_good_config from %s as part of initial reconcile",
            DEFAULT_CONFIG_PATH,
        )
        return True

    def _has_config_drift(self) -> bool:
        """Return True when the on-disk config differs from the last good config."""
        if not self._stored.last_good_config:
            return False
        on_disk = self._read_config_from_disk()
        if on_disk is None:
            return True
        return on_disk != self._stored.last_good_config

    def _read_config_from_disk(self) -> str | None:
        """Read the on-disk config text, returning None if unreadable."""
        path = Path(DEFAULT_CONFIG_PATH)
        try:
            return path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return None
        except OSError as exc:
            logger.warning("Failed to read Alloy config from disk: %s", exc)
            return None

    def _config_drift_message(self) -> str:
        return f"Manual Alloy config change detected at {DEFAULT_CONFIG_PATH}"

    def _is_drift_status(self) -> bool:
        return (
            isinstance(self.unit.status, ops.MaintenanceStatus)
            and self.unit.status.message == self._config_drift_message()
        )

    def _is_service_down_status(self) -> bool:
        return (
            isinstance(self.unit.status, ops.MaintenanceStatus)
            and self.unit.status.message == "Alloy service not running"
        )

    def _is_grafana_cloud_status(self) -> bool:
        """Return whether the current status is from a Grafana Cloud connectivity check."""
        return isinstance(
            self.unit.status, ops.BlockedStatus
        ) and self.unit.status.message.startswith("Grafana Cloud ")

    def _render_config_text(self, *, manual_metrics_jobs: list[MetricsScrapeJob]) -> str:
        override = str(self.config.get("config-override", "")).strip()
        if override:
            return override
        builder = ConfigBuilder(
            loki_endpoints=self._loki_endpoint_urls(),
            remote_write_endpoints=self._remote_write_endpoint_urls(),
            metrics_scrape_jobs=self._active_metrics_scrape_jobs(
                manual_metrics_jobs=manual_metrics_jobs
            ),
            systemd_units=self._systemd_units(),
            journal_kernel=self._journal_kernel_enabled(),
            journal_match_expressions=self._journal_match_expressions(),
            live_debugging=self._live_debugging_enabled(),
            enable_syslog_receivers=self._syslog_receivers_enabled(),
            syslog_drop_access_logs=self._syslog_drop_access_logs(),
            syslog_drop_expressions=self._syslog_drop_expressions(),
            syslog_rate_limit=self._syslog_rate_limit(),
            syslog_rate_burst=self._syslog_rate_burst(),
            receiver_hostname=self._syslog_receiver_hostname(),
            receiver_ip=self._syslog_receiver_ip(),
            topology_labels=self._topology.as_dict(
                remapped_keys={
                    "model": "juju_model",
                    "model_uuid": "juju_model_uuid",
                    "application": "juju_application",
                    "unit": "juju_unit",
                    "charm_name": "juju_charm",
                }
            ),
            log_source_groups=self._machine_observability_log_source_groups(),
            network_pipeline=self._network_pipeline,
            self_metrics_address=self._self_metrics_address(),
            syslog_parsers=self._network_features.parsers,
            location_labels=self._location_labels,
            log_level=str(self.config.get("log-level", "info")),
            collector_id=(
                self._stored.last_network_collector
                if self.config.get("network-monitoring-enabled")
                else ""
            ),
        )
        return f"{alloy.GENERATED_CONFIG_HEADER}{builder.build()}"

    def _validate_config_text(self, config_text: str) -> bool:
        """Validate Alloy config by writing to a temp file and running verify."""
        tmp_path = None
        try:
            with tempfile.NamedTemporaryFile("w", delete=False, suffix=".alloy") as tmp:
                tmp.write(config_text)
                tmp_path = Path(tmp.name)
            alloy.verify_config(config_path=tmp_path)
        except Exception as exc:  # noqa: BLE001
            logger.warning("Alloy config verification failed (%s)", type(exc).__name__)
            return False
        finally:
            if tmp_path and tmp_path.exists():
                tmp_path.unlink()
        return True

    def _persist_failed_config(self, config_text: str) -> Path | None:
        """Persist a failed config to /tmp for debugging."""
        try:
            with tempfile.NamedTemporaryFile(
                "w",
                delete=False,
                prefix="alloy-config-invalid-",
                suffix=".alloy",
                dir="/tmp",
            ) as tmp:
                tmp.write(config_text)
                failed_path = Path(tmp.name)
            logger.warning("Invalid Alloy config kept at %s", failed_path)
            return failed_path
        except Exception as exc:  # noqa: BLE001
            logger.warning("Failed to persist invalid config: %s", exc)
            return None

    def _write_alloy_systemd_unit_defaults(self) -> None:
        """Write /etc/default/alloy based on charm config."""
        alloy.write_service_defaults(self._desired_custom_args())

    def _desired_custom_args(self) -> str:
        return str(self.config.get("custom_args", "--server.http.listen-addr=127.0.0.1:6987"))

    def _self_metrics_address(self) -> str:
        """Scrape the local listener selected by the actual service arguments."""
        if self._live_debugging_enabled():
            return "127.0.0.1:12345"
        arguments = shlex.split(self._desired_custom_args())
        address = "127.0.0.1:12345"  # Alloy's default listener.
        for index, argument in enumerate(arguments):
            if argument.startswith("--server.http.listen-addr="):
                address = argument.split("=", 1)[1]
            elif argument == "--server.http.listen-addr" and index + 1 < len(arguments):
                address = arguments[index + 1]
        host, _, port = address.rpartition(":")
        if host in {"0.0.0.0", "", "[::]"}:
            host = "127.0.0.1"
        return f"{host}:{port}"

    def _apply_live_debugging(self, *, baseline_custom_args: str) -> None:
        """Toggle live debugging by updating CUSTOM_ARGS, preserving prior value."""
        enabled = self._live_debugging_enabled()
        if enabled:
            if not self._stored.livedebug_prev_custom_args_set:
                previous = alloy.read_custom_args()
                self._stored.livedebug_prev_custom_args = previous or baseline_custom_args
                self._stored.livedebug_prev_custom_args_set = True
            else:
                self._stored.livedebug_prev_custom_args = baseline_custom_args
            alloy.write_custom_args("--server.http.listen-addr=0.0.0.0:12345")
            return
        if self._stored.livedebug_prev_custom_args_set:
            previous = self._stored.livedebug_prev_custom_args or baseline_custom_args
            alloy.write_custom_args(previous or None)
            self._stored.livedebug_prev_custom_args = ""
            self._stored.livedebug_prev_custom_args_set = False

    def _live_debugging_enabled(self) -> bool:
        """Return True when live debugging is enabled in config."""
        return bool(self.config.get("alloy-livedebugging", False))

    def _syslog_receivers_enabled(self) -> bool:
        """Return True when syslog receiver listeners should be enabled."""
        return bool(self.config.get("enable-syslogreceivers", False))

    def _journal_kernel_enabled(self) -> bool:
        """Return True when host kernel journal messages should be collected."""
        return bool(self.config.get("journal-kernel", False))

    def _journal_match_expressions(self) -> list[str]:
        """Return raw additional journald match expressions from charm config."""
        raw = str(self.config.get("journal-match-expressions", "")).strip()
        if not raw:
            return []
        return [line.strip() for line in raw.splitlines() if line.strip()]

    def _syslog_drop_access_logs(self) -> bool:
        """Return True when common remote syslog access logs should be dropped."""
        return bool(self.config.get("syslog-drop-access-logs", False))

    def _syslog_drop_expressions(self) -> list[str]:
        """Return additional remote syslog drop expressions from charm config."""
        raw = str(self.config.get("syslog-drop-expressions", "")).strip()
        if not raw:
            return []
        return [line.strip() for line in raw.splitlines() if line.strip()]

    def _syslog_rate_limit(self) -> int:
        """Return the remote syslog rate limit, disabled when non-positive."""
        try:
            return max(int(self.config.get("syslog-rate-limit", 0)), 0)
        except (TypeError, ValueError):
            return 0

    def _syslog_rate_burst(self) -> int:
        """Return the remote syslog burst limit, disabled when non-positive."""
        try:
            return max(int(self.config.get("syslog-rate-burst", 0)), 0)
        except (TypeError, ValueError):
            return 0

    def _syslog_receiver_hostname(self) -> str:
        """Return hostname label for syslog receiver listeners."""
        try:
            return socket.getfqdn()
        except OSError:
            return self.unit.name

    def _syslog_receiver_ip(self) -> str:
        """Return ingress IP label for syslog receiver listeners."""
        for endpoint in (SYSLOG_RELATION_NAME, ""):
            try:
                binding = self.model.get_binding(endpoint)
            except (ops.ModelError, KeyError):
                continue
            if binding and binding.network.ingress_address:
                return str(binding.network.ingress_address)
        return "0.0.0.0"

    def _refresh_syslog_receiver_relations(self) -> None:
        """Publish syslog receiver details to related requirers."""
        if SYSLOG_RELATION_NAME not in self.model.relations:
            return

        unit_payload = self._syslog_receiver_relation_payload()
        app_payload = self._syslog_receiver_relation_payload(include_address=True)

        for relation in self.model.relations[SYSLOG_RELATION_NAME]:
            self._sync_relation_databag(relation.data[self.unit], unit_payload)
            if self.unit.is_leader():
                self._sync_relation_databag(relation.data[self.app], app_payload)

    def _syslog_receiver_relation_payload(self, *, include_address: bool = True) -> dict[str, str]:
        """Return relation data describing the current syslog receiver state."""
        ready, reason = self._syslog_receiver_ready_state()
        payload = {
            "ready": "true" if ready else "false",
            "reason": reason,
            "recommended-protocol": SYSLOG_RECOMMENDED_PROTOCOL,
        }
        if self._syslog_receivers_enabled():
            payload["port"] = SYSLOG_RECEIVER_PORT
            payload["protocols"] = SYSLOG_RECEIVER_PROTOCOLS
            if include_address:
                payload["address"] = self._syslog_receiver_ip()
        return payload

    def _syslog_receiver_ready_state(self) -> tuple[bool, str]:
        """Return readiness and reason for the syslog receiver relation."""
        if not self._syslog_receivers_enabled():
            return False, "syslog receivers disabled"
        if not self._loki_endpoint_urls():
            return False, "waiting for send-loki-logs relation"
        return True, "ready"

    @staticmethod
    def _sync_relation_databag(databag: ops.RelationDataContent, desired: dict[str, str]) -> None:
        """Update a relation databag in place, removing stale keys."""
        current_keys = set(databag)
        desired_keys = set(desired)
        for key in current_keys - desired_keys:
            databag.pop(key, None)
        for key, value in desired.items():
            if databag.get(key) != value:
                databag[key] = value

    def _grafana_cloud_metrics_endpoints(self) -> list[OutboundEndpoint]:
        """Return Grafana Cloud remote-write endpoints."""
        if not self.grafana_cloud.prometheus_ready:
            return []
        credentials = self.grafana_cloud.prometheus_credentials
        return [
            OutboundEndpoint(
                url=self.grafana_cloud.prometheus_url,
                username=credentials.username if credentials else "",
                password=credentials.password if credentials else "",
                tls_ca_pem=self.grafana_cloud.tls_ca,
            )
        ]

    def _grafana_cloud_loki_endpoints(self) -> list[OutboundEndpoint]:
        """Return Grafana Cloud Loki endpoints."""
        if not self.grafana_cloud.loki_ready:
            return []
        credentials = self.grafana_cloud.loki_credentials
        return [
            OutboundEndpoint(
                url=self.grafana_cloud.loki_url,
                username=credentials.username if credentials else "",
                password=credentials.password if credentials else "",
                tls_ca_pem=self.grafana_cloud.tls_ca,
            )
        ]

    def _loki_endpoint_urls(self) -> list[OutboundEndpoint]:
        """Return outbound Loki endpoints from all configured relations."""
        relation_endpoints = [
            OutboundEndpoint(url=endpoint["url"])
            for endpoint in self._loki_consumer.loki_endpoints
            if endpoint.get("url")
        ]
        return dedupe_endpoints([*relation_endpoints, *self._grafana_cloud_loki_endpoints()])

    def _remote_write_endpoint_urls(self) -> list[OutboundEndpoint]:
        """Return outbound remote-write endpoints from all configured relations."""
        relation_endpoints = [
            OutboundEndpoint(url=endpoint["url"])
            for endpoint in self._remote_write_consumer.endpoints
            if endpoint.get("url")
        ]
        return dedupe_endpoints([*relation_endpoints, *self._grafana_cloud_metrics_endpoints()])

    def _active_metrics_scrape_jobs(
        self,
        *,
        manual_metrics_jobs: list[MetricsScrapeJob],
    ) -> list[MetricsScrapeJob]:
        """Return rendered scrape jobs only when an upstream exists."""
        if not self._remote_write_endpoint_urls():
            return []
        return [
            *self._translated_metrics_scrape_jobs(),
            *manual_metrics_jobs,
            *self._machine_observability_metrics_scrape_jobs(),
        ]

    def _manual_metrics_scrape_jobs(self) -> list[MetricsScrapeJob]:
        """Parse manual operator-defined scrape jobs from charm config."""
        if str(self.config.get("config-override", "")).strip():
            return []
        return parse_manual_metrics_jobs(
            str(self.config.get("manual-metrics-jobs", "")),
            topology_labels=self._topology.as_dict(
                remapped_keys={
                    "model": "juju_model",
                    "model_uuid": "juju_model_uuid",
                    "application": "juju_application",
                    "unit": "juju_unit",
                    "charm_name": "juju_charm",
                }
            ),
        )

    def _translated_metrics_scrape_jobs(self) -> list[MetricsScrapeJob]:
        """Translate a supported subset of Prometheus scrape jobs into Alloy jobs."""
        translated: list[MetricsScrapeJob] = []
        for job in self._metrics_consumer.jobs():
            translated_job = self._translate_metrics_scrape_job(job)
            if translated_job is not None:
                translated.append(translated_job)
        return translated

    def _translate_metrics_scrape_job(
        self,
        job: Mapping[str, object],
    ) -> MetricsScrapeJob | None:
        """Translate one supported Prometheus scrape job into an Alloy scrape job."""
        unsupported_job_keys = set(job) - {
            "job_name",
            "metrics_path",
            "scheme",
            "scrape_interval",
            "scrape_timeout",
            "static_configs",
            "relabel_configs",
            "tls_config",
        }
        if unsupported_job_keys:
            logger.warning(
                "Skipping unsupported scrape job %s with fields %s",
                job.get("job_name", "<unnamed>"),
                sorted(unsupported_job_keys),
            )
            return None

        raw_static_configs = job.get("static_configs", [])
        if not isinstance(raw_static_configs, list):
            logger.warning(
                "Skipping scrape job with invalid static_configs: %r",
                raw_static_configs,
            )
            return None

        scrape_targets = self._translate_static_configs(job, raw_static_configs)
        if not scrape_targets:
            logger.warning(
                "Skipping scrape job %s due to invalid or empty targets",
                job.get("job_name", "<unnamed>"),
            )
            return None

        return MetricsScrapeJob(
            job_name=self._metrics_job_name(job),
            targets=scrape_targets,
            metrics_path=str(job.get("metrics_path", "/metrics")),
            scheme=str(job.get("scheme", "http")),
            scrape_interval=str(job.get("scrape_interval", "")),
            scrape_timeout=str(job.get("scrape_timeout", "")),
            tls_config=self._translate_tls_config(job),
        )

    def _metrics_job_name(self, job: Mapping[str, object]) -> str:
        """Return the rendered job name, honoring any provider-supplied unit override."""
        labels = self._scrape_job_labels(job)
        override = self._metrics_job_name_overrides().get(
            (
                labels.get("juju_model_uuid", ""),
                labels.get("juju_application", ""),
                labels.get("juju_unit", ""),
            )
        )
        if override:
            return override
        return str(job.get("job_name", "metrics-endpoint"))

    def _metrics_job_name_overrides(self) -> dict[tuple[str, str, str], str]:
        """Return optional per-unit job-name overrides published by remote scrape providers."""
        overrides: dict[tuple[str, str, str], str] = {}
        for relation in self.model.relations.get(METRICS_RELATION_NAME, []):
            remote_app = relation.app
            if remote_app is None:
                continue
            try:
                scrape_metadata = json.loads(
                    relation.data[remote_app].get("scrape_metadata", "{}")
                )
            except json.JSONDecodeError:
                continue
            model_uuid = str(scrape_metadata.get("model_uuid", "")).strip()
            application = str(scrape_metadata.get("application", "")).strip()
            if not (model_uuid and application):
                continue
            for unit in relation.units:
                unit_data = relation.data[unit]
                override = unit_data.get(METRICS_JOB_NAME_FIELD, "").strip()
                unit_name = unit_data.get("prometheus_scrape_unit_name", "").strip() or unit.name
                if override and unit_name:
                    overrides[(model_uuid, application, unit_name)] = override
        return overrides

    @staticmethod
    def _scrape_job_labels(job: Mapping[str, object]) -> dict[str, str]:
        """Return the first static_config label mapping from a translated scrape job."""
        static_configs = job.get("static_configs", [])
        if not isinstance(static_configs, list) or not static_configs:
            return {}
        first_static_config = static_configs[0]
        if not isinstance(first_static_config, Mapping):
            return {}
        labels = first_static_config.get("labels", {})
        if not isinstance(labels, Mapping):
            return {}
        return {str(key): str(value) for key, value in labels.items()}

    def _translate_tls_config(
        self,
        job: Mapping[str, object],
    ) -> dict[str, str | bool]:
        raw_tls_config = job.get("tls_config", {})
        if raw_tls_config in ({}, None):
            return {}
        if not isinstance(raw_tls_config, Mapping):
            logger.warning(
                "Skipping scrape job %s due to invalid tls_config: %r",
                job.get("job_name", "<unnamed>"),
                raw_tls_config,
            )
            return {}

        translated: dict[str, str | bool] = {}
        for raw_key in (
            "insecure_skip_verify",
            "server_name",
            "ca_file",
            "cert_file",
            "key_file",
        ):
            value = raw_tls_config.get(raw_key)
            if value in ("", None):
                continue
            if raw_key == "insecure_skip_verify":
                translated[raw_key] = bool(value)
                continue
            if not isinstance(value, str):
                logger.warning(
                    "Skipping unsupported tls_config field %s on scrape job %s",
                    raw_key,
                    job.get("job_name", "<unnamed>"),
                )
                continue
            translated[self._translate_tls_config_key(raw_key, value)] = value
        return translated

    @staticmethod
    def _translate_tls_config_key(key: str, value: str) -> str:
        if key == "ca_file" and "-----BEGIN" in value:
            return "ca_pem"
        if key == "cert_file" and "-----BEGIN" in value:
            return "cert_pem"
        if key == "key_file" and "-----BEGIN" in value:
            return "key_pem"
        return key

    def _translate_static_configs(
        self,
        job: Mapping[str, object],
        raw_static_configs: list[object],
    ) -> list[ScrapeTarget]:
        """Translate static_configs into Alloy targets for one scrape job."""
        scrape_targets: list[ScrapeTarget] = []
        for static_config in raw_static_configs:
            if not isinstance(static_config, Mapping):
                return []
            unsupported_static_keys = set(static_config) - {"targets", "labels"}
            if unsupported_static_keys:
                logger.warning(
                    "Skipping unsupported scrape job %s static config fields %s",
                    job.get("job_name", "<unnamed>"),
                    sorted(unsupported_static_keys),
                )
                return []
            labels = static_config.get("labels", {})
            if labels is None:
                labels = {}
            if not isinstance(labels, Mapping):
                return []
            targets = static_config.get("targets", [])
            if not isinstance(targets, list):
                return []
            for target in targets:
                if not isinstance(target, str):
                    return []
                scrape_targets.append(
                    ScrapeTarget(
                        address=self._normalize_scrape_target(target),
                        labels={str(key): str(value) for key, value in labels.items()},
                    )
                )
        return scrape_targets

    @staticmethod
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

    def _post_config_status(
        self,
        active_message: str,
        *,
        manual_metrics_jobs: list[MetricsScrapeJob] | None = None,
    ) -> ops.StatusBase:
        """Return the desired post-config status for the current relation state."""
        if self._network_status is not None:
            return self._network_status
        if (
            self._metrics_consumer.jobs()
            or manual_metrics_jobs
            or self._machine_observability_metrics_scrape_jobs()
        ) and not self._remote_write_endpoint_urls():
            return ops.WaitingStatus(
                "Waiting for remote write before enabling manual or related metrics scraping"
            )
        return ops.ActiveStatus(active_message)

    def _grafana_cloud_status_error(self) -> str | None:
        """Return the first Grafana Cloud connectivity failure, if any."""
        for endpoint in self._grafana_cloud_metrics_endpoints():
            ok, reason = probe_endpoint(endpoint)
            if not ok:
                return f"Grafana Cloud metrics connectivity failed: {reason}"
        for endpoint in self._grafana_cloud_loki_endpoints():
            ok, reason = probe_endpoint(endpoint)
            if not ok:
                return f"Grafana Cloud logs connectivity failed: {reason}"
        return None

    def _systemd_units(self) -> list[str]:
        raw = str(self.config.get("systemd-units", "")).strip()
        if not raw:
            return []
        tokens = raw.replace("\n", ",").split(",")
        return [token.strip() for token in tokens if token.strip()]

    def _machine_observability_contract_error(self) -> str | None:
        """Return the first invalid machine-observability contract error."""
        for relation in self.model.relations.get("machine-observability", []):
            remote_app = relation.app
            if remote_app is None:
                continue
            raw_payload = relation.data[remote_app].get("payload", "").strip()
            if not raw_payload:
                continue
            try:
                payload = self._machine_observability_telemetry_payload(relation)
            except (ValidationError, json.JSONDecodeError, TypeError, ValueError):
                return (
                    "machine-observability payload validation failed for "
                    f"{remote_app.name}: schema"
                )
            if payload.schema_version not in (2, 3) or payload.source_topology is None:
                return (
                    "machine-observability from "
                    f"{remote_app.name} requires schema_version 2 or 3 with source_topology"
                )
        return None

    @staticmethod
    def _machine_observability_telemetry_payload(
        relation: ops.Relation,
    ) -> MachineObservabilityPayload:
        """Validate telemetry independently from v3 artifacts using an artifact-free copy."""
        remote_app = relation.app
        raw_payload = relation.data[remote_app].get("payload", "{}") if remote_app else "{}"
        parsed = parse_machine_observability_payload_json(raw_payload)
        if not isinstance(parsed, dict):
            raise ValueError("schema")
        if parsed.get("schema_version", 1) == 3:
            parsed = {**parsed, "artifacts": []}
        return MachineObservabilityPayload.model_validate(parsed)

    def _machine_observability_sources(self) -> list[MachineObservabilitySource]:
        """Return translated machine-observability sources from related principals."""
        sources: list[MachineObservabilitySource] = []
        for relation in self.model.relations.get("machine-observability", []):
            remote_app = relation.app
            if remote_app is None:
                continue
            raw_payload = relation.data[remote_app].get("payload", "").strip()
            if not raw_payload:
                continue
            payload = self._machine_observability_telemetry_payload(relation)
            sources.append(translate_machine_observability_payload(payload))
        return sources

    def _machine_observability_metrics_scrape_jobs(self) -> list[MetricsScrapeJob]:
        """Return translated metrics scrape jobs from machine-observability relations."""
        jobs: list[MetricsScrapeJob] = []
        for source in self._machine_observability_sources():
            jobs.extend(source.metrics_scrape_jobs)
        return jobs

    def _machine_observability_log_source_groups(self) -> list[LogSourceGroup]:
        """Return translated log source groups from machine-observability relations."""
        groups: list[LogSourceGroup] = []
        for source in self._machine_observability_sources():
            if source.log_source_group is not None:
                groups.append(source.log_source_group)
        return groups


if __name__ == "__main__":
    ops.main(AlloyCharm)
