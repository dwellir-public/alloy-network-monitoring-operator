import base64
import json
import sys
import zlib
from contextlib import ExitStack
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import PropertyMock, patch

import ops
import pytest
from ops import testing

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "lib"))

from charms.dwellir_observability.v0.machine_observability import (
    MAX_SERIALIZED_PAYLOAD_BYTES,
    PayloadTooLargeError,
    encode_artifact,
)

from charm import AlloyCharm, PayloadDecodeError, parse_machine_observability_payload_json


@pytest.fixture(autouse=True)
def _accept_backend_rules(monkeypatch):
    monkeypatch.setattr(AlloyCharm, "_validate_artifact_rules", lambda *_args, **_kwargs: True)


DEFAULT_ARGS = "--server.http.listen-addr=0.0.0.0:6987"


def test_raw_payload_parser_allows_exact_limit_and_rejects_one_extra_byte():
    base = '{"schema_version":3,"artifacts":[]}'
    exact = base + " " * (MAX_SERIALIZED_PAYLOAD_BYTES - len(base.encode("utf-8")))

    assert parse_machine_observability_payload_json(exact) == {
        "schema_version": 3,
        "artifacts": [],
    }
    with pytest.raises(PayloadTooLargeError):
        parse_machine_observability_payload_json(exact + " ")


@pytest.mark.parametrize(
    "raw",
    ["{", '{"schema_version":3,"artifacts":' + "[" * 10_000 + "]" * 10_000 + "}"],
)
def test_raw_payload_parser_normalizes_invalid_and_deep_json(raw):
    with pytest.raises(PayloadDecodeError):
        parse_machine_observability_payload_json(raw)


def _patch_runtime():
    return (
        patch("charm.alloy.ensure_config_dir_permissions"),
        patch("charm.alloy.verify_config"),
        patch("charm.alloy.restart"),
        patch("charm.alloy.reload"),
        patch("charm.AlloyCharm._syslog_receiver_hostname", return_value="alloy-host"),
        patch("charm.AlloyCharm._syslog_receiver_ip", return_value="10.0.0.10"),
        patch("charm.alloy.read_custom_args", return_value=DEFAULT_ARGS),
        patch("charm.alloy.write_custom_args"),
        patch("charm.AlloyCharm._write_alloy_systemd_unit_defaults"),
    )


def test_machine_observability_v2_relation_renders_metrics_and_logs():
    seen: dict[str, str] = {}
    with ExitStack() as stack:
        for manager in _patch_runtime():
            stack.enter_context(manager)
        stack.enter_context(
            patch(
                "charm.PrometheusRemoteWriteConsumer.endpoints",
                new_callable=PropertyMock,
                return_value=[{"url": "http://mimir:9009/api/v1/push"}],
            )
        )
        stack.enter_context(
            patch(
                "charm.LokiPushApiConsumer.loki_endpoints",
                new_callable=PropertyMock,
                return_value=[{"url": "http://loki:3100/loki/api/v1/push"}],
            )
        )
        stack.enter_context(
            patch(
                "charm.alloy.write_config_text",
                side_effect=lambda config_text, **_: seen.__setitem__("config", config_text),
            )
        )
        harness = testing.Harness(AlloyCharm)
        harness.begin()

        relation_id = harness.add_relation("machine-observability", "op-node")
        harness.add_relation_unit(relation_id, "op-node/0")
        harness.update_relation_data(
            relation_id,
            "op-node",
            {
                "payload": json.dumps(
                    {
                        "schema_version": 2,
                        "charm_name": "op-node",
                        "source_topology": {
                            "model": "base-mainnet-ovh-us-west-2",
                            "model_uuid": "00000000-0000-4000-8000-000000000042",
                            "application": "op-node",
                            "unit": "op-node/0",
                            "charm_name": "op-node",
                        },
                        "metrics_endpoints": [
                            {
                                "targets": ["localhost:7300"],
                                "path": "/metrics",
                                "scheme": "http",
                                "interval": "",
                                "timeout": "",
                                "tls": {},
                            }
                        ],
                        "systemd_units": ["opnode.service"],
                        "journal_match_expressions": [],
                        "log_files": [
                            {
                                "include": ["/var/log/op-node/*.log"],
                                "exclude": ["/var/log/op-node/debug.log"],
                                "attributes": {"service_name": "op-node"},
                            }
                        ],
                    }
                )
            },
        )

    rendered = seen["config"]
    assert 'job_name = "op-node_0"' in rendered
    assert '__address__ = "localhost:7300"' in rendered
    assert 'juju_application = "op-node"' in rendered
    assert 'juju_unit = "op-node/0"' in rendered
    assert 'juju_charm = "op-node"' in rendered
    assert 'matches = "_SYSTEMD_UNIT=opnode.service"' in rendered
    assert "/var/log/op-node/*.log" in rendered
    assert "/var/log/op-node/debug.log" in rendered


def test_v2_telemetry_with_both_standard_relations_publishes_empty_rules_and_metadata():
    seen: dict[str, str] = {}
    model_name = "alloy-model"
    model_uuid = "00000000-0000-4000-8000-000000000111"
    with ExitStack() as stack:
        for manager in _patch_runtime():
            stack.enter_context(manager)
        stack.enter_context(
            patch(
                "charm.PrometheusRemoteWriteConsumer.endpoints",
                new_callable=PropertyMock,
                return_value=[{"url": "http://mimir:9009/api/v1/push"}],
            )
        )
        stack.enter_context(
            patch(
                "charm.LokiPushApiConsumer.loki_endpoints",
                new_callable=PropertyMock,
                return_value=[{"url": "http://loki:3100/loki/api/v1/push"}],
            )
        )
        stack.enter_context(
            patch(
                "charm.alloy.write_config_text",
                side_effect=lambda config_text, **_: seen.__setitem__("config", config_text),
            )
        )
        harness = testing.Harness(AlloyCharm)
        harness.set_leader(True)
        harness.set_model_name(model_name)
        harness.set_model_uuid(model_uuid)
        harness.begin()

        prometheus = harness.add_relation("send-remote-write", "mimir")
        loki = harness.add_relation("send-loki-logs", "loki")
        grafana_cloud = harness.add_relation("grafana-cloud-config", "grafana-cloud")
        machine = harness.add_relation("machine-observability", "op-node")
        harness.add_relation_unit(machine, "op-node/0")
        harness.update_relation_data(
            machine,
            "op-node",
            {
                "payload": json.dumps(
                    {
                        "schema_version": 2,
                        "charm_name": "op-node",
                        "source_topology": {
                            "model": "principal-model",
                            "model_uuid": "principal-uuid",
                            "application": "op-node",
                            "unit": "op-node/0",
                            "charm_name": "op-node",
                        },
                        "metrics_endpoints": [{"targets": ["localhost:7300"]}],
                        "systemd_units": ["opnode.service"],
                        "journal_match_expressions": [],
                        "log_files": [
                            {
                                "include": ["/var/log/op-node/*.log"],
                                "exclude": ["/var/log/op-node/debug.log"],
                                "attributes": {"service_name": "op-node"},
                            }
                        ],
                    }
                )
            },
        )

    expected_metadata = json.dumps(
        {
            "application": harness.charm.app.name,
            "model": model_name,
            "model_uuid": model_uuid,
            "unit": f"{harness.charm.app.name}/0",
        },
        sort_keys=True,
    )
    for relation_id in (prometheus, loki):
        relation_data = harness.get_relation_data(relation_id, harness.charm.app.name)
        assert relation_data["alert_rules"] == '{"groups":[]}'
        assert relation_data["metadata"] == expected_metadata
    assert "alert_rules" not in harness.get_relation_data(grafana_cloud, harness.charm.app.name)

    rendered = seen["config"]
    assert "http://mimir:9009/api/v1/push" in rendered
    assert "http://loki:3100/loki/api/v1/push" in rendered
    assert 'job_name = "op-node_0"' in rendered
    assert '__address__ = "localhost:7300"' in rendered
    assert 'juju_model = "principal-model"' in rendered
    assert 'juju_model_uuid = "principal-uuid"' in rendered
    assert 'juju_application = "op-node"' in rendered
    assert 'juju_unit = "op-node/0"' in rendered
    assert 'matches = "_SYSTEMD_UNIT=opnode.service"' in rendered
    assert "/var/log/op-node/*.log" in rendered
    assert "/var/log/op-node/debug.log" in rendered


def test_machine_observability_v1_relation_is_blocked():
    with ExitStack() as stack:
        for manager in _patch_runtime():
            stack.enter_context(manager)
        stack.enter_context(patch("charm.alloy.write_config_text"))
        harness = testing.Harness(AlloyCharm)
        harness.begin()

        relation_id = harness.add_relation("machine-observability", "polkadot")
        harness.add_relation_unit(relation_id, "polkadot/0")
        harness.update_relation_data(
            relation_id,
            "polkadot",
            {
                "payload": json.dumps(
                    {
                        "schema_version": 1,
                        "charm_name": "polkadot",
                        "metrics_endpoints": [],
                        "systemd_units": ["snap.polkadot.polkadot.service"],
                        "journal_match_expressions": [],
                        "log_files": [],
                    }
                )
            },
        )

    assert harness.charm.unit.status.name == "blocked"
    assert "schema_version 2" in harness.charm.unit.status.message


def _rule_artifact(artifact_type, artifact_id, group_name, *, checksum_valid=True):
    artifact = encode_artifact(
        artifact_type=artifact_type,
        artifact_id=artifact_id,
        content=json.dumps(
            {
                "groups": [
                    {
                        "name": group_name,
                        "rules": [{"alert": "Down", "expr": "up{%%juju_topology%%} == 0"}],
                    }
                ]
            }
        ).encode(),
    ).model_dump()
    if not checksum_valid:
        artifact["sha256"] = "0" * 64
    return artifact


def _v3_payload(*artifacts):
    return {
        "schema_version": 3,
        "source_topology": {
            "model": "principal-model",
            "model_uuid": "principal-uuid",
            "application": "polkadot",
            "unit": "polkadot/0",
            "charm_name": "polkadot",
        },
        "metrics_endpoints": [],
        "systemd_units": [],
        "journal_match_expressions": [],
        "log_files": [],
        "artifacts": list(artifacts),
    }


def test_v3_rules_and_telemetry_publish_to_standard_relations_with_principal_topology():
    seen: dict[str, str] = {}
    with ExitStack() as stack:
        for manager in _patch_runtime():
            stack.enter_context(manager)
        stack.enter_context(
            patch(
                "charm.PrometheusRemoteWriteConsumer.endpoints",
                new_callable=PropertyMock,
                return_value=[{"url": "http://mimir:9009/api/v1/push"}],
            )
        )
        stack.enter_context(
            patch(
                "charm.LokiPushApiConsumer.loki_endpoints",
                new_callable=PropertyMock,
                return_value=[{"url": "http://loki:3100/loki/api/v1/push"}],
            )
        )
        stack.enter_context(
            patch(
                "charm.alloy.write_config_text",
                side_effect=lambda config_text, **_: seen.__setitem__("config", config_text),
            )
        )
        harness = testing.Harness(AlloyCharm)
        harness.set_leader(True)
        harness.begin()
        prometheus = harness.add_relation("send-remote-write", "mimir")
        loki = harness.add_relation("send-loki-logs", "loki")
        machine = harness.add_relation("machine-observability", "polkadot")
        payload = _v3_payload(
            _rule_artifact("prometheus_alert_rules", "metrics", "Metrics"),
            _rule_artifact("loki_alert_rules", "logs", "Logs"),
        )
        payload["metrics_endpoints"] = [{"targets": ["localhost:9615"]}]
        payload["systemd_units"] = ["polkadot.service"]

        harness.update_relation_data(machine, "polkadot", {"payload": json.dumps(payload)})

    assert 'job_name = "polkadot_0"' in seen["config"]
    assert 'juju_model_uuid = "principal-uuid"' in seen["config"]
    prometheus_data = harness.get_relation_data(prometheus, harness.charm.app.name)
    loki_data = harness.get_relation_data(loki, harness.charm.app.name)
    assert len(json.loads(prometheus_data["alert_rules"])["groups"]) == 1
    assert len(json.loads(loki_data["alert_rules"])["groups"]) == 1
    assert json.loads(prometheus_data["metadata"])["application"] == "alloy-network-monitoring"
    assert json.loads(loki_data["metadata"])["application"] == "alloy-network-monitoring"


def test_config_changed_republishes_prometheus_and_loki_rules_after_consumers(monkeypatch):
    monkeypatch.setattr(
        AlloyCharm, "_configure", lambda *_args, **_kwargs: ops.ActiveStatus("configured")
    )
    harness = testing.Harness(AlloyCharm)
    harness.set_leader(True)
    harness.begin()
    prometheus = harness.add_relation("send-remote-write", "mimir")
    loki = harness.add_relation("send-loki-logs", "loki")
    machine = harness.add_relation("machine-observability", "polkadot")
    harness.update_relation_data(
        machine,
        "polkadot",
        {
            "payload": json.dumps(
                _v3_payload(
                    _rule_artifact("prometheus_alert_rules", "metrics", "Metrics"),
                    _rule_artifact("loki_alert_rules", "logs", "Logs"),
                )
            )
        },
    )

    harness.update_config({"log-level": "debug"})

    assert (
        len(
            json.loads(
                harness.get_relation_data(prometheus, harness.charm.app.name)["alert_rules"]
            )["groups"]
        )
        == 1
    )
    assert (
        len(
            json.loads(harness.get_relation_data(loki, harness.charm.app.name)["alert_rules"])[
                "groups"
            ]
        )
        == 1
    )


def test_config_changed_reconciles_rules_once_after_configure(monkeypatch):
    calls: list[str] = []
    monkeypatch.setattr(
        AlloyCharm,
        "_configure",
        lambda *_args, **_kwargs: calls.append("configure") or ops.ActiveStatus("configured"),
    )
    monkeypatch.setattr(
        AlloyCharm,
        "_reconcile_rule_groups",
        lambda *_args, **_kwargs: calls.append("rules"),
    )
    harness = testing.Harness(AlloyCharm)
    harness.begin()
    calls.clear()

    harness.update_config({"log-level": "debug"})

    assert calls == ["configure", "configure", "rules"]


def test_machine_relation_config_exception_does_not_starve_rule_reconcile_and_recovery(
    monkeypatch,
):
    fail_config = False

    def configure(*_args, **_kwargs):
        if fail_config:
            raise RuntimeError("configuration failed")
        return ops.ActiveStatus("configured")

    monkeypatch.setattr(AlloyCharm, "_configure", configure)
    harness = testing.Harness(AlloyCharm)
    harness.set_leader(True)
    harness.begin()
    machine = harness.add_relation("machine-observability", "polkadot")
    prometheus = harness.add_relation("send-remote-write", "mimir")
    harness.update_relation_data(
        machine,
        "polkadot",
        {
            "payload": json.dumps(
                _v3_payload(_rule_artifact("prometheus_alert_rules", "owned", "RULE-V1"))
            )
        },
    )

    fail_config = True
    with pytest.raises(RuntimeError, match="configuration failed"):
        harness.update_relation_data(
            machine,
            "polkadot",
            {
                "payload": json.dumps(
                    _v3_payload(_rule_artifact("prometheus_alert_rules", "owned", "RULE-V2"))
                )
            },
        )
    groups = json.loads(
        harness.get_relation_data(prometheus, harness.charm.app.name)["alert_rules"]
    )["groups"]
    assert groups[0]["name"].endswith("owned-RULE-V2")

    fail_config = False
    harness.update_relation_data(
        machine,
        "polkadot",
        {
            "payload": json.dumps(
                _v3_payload(_rule_artifact("prometheus_alert_rules", "owned", "RULE-V3"))
            )
        },
    )
    groups = json.loads(
        harness.get_relation_data(prometheus, harness.charm.app.name)["alert_rules"]
    )["groups"]
    assert groups[0]["name"].endswith("owned-RULE-V3")
    assert harness.charm.unit.status.name == "active"


def test_machine_relation_event_configures_then_reconciles_rules_once(monkeypatch):
    calls: list[str] = []
    monkeypatch.setattr(
        AlloyCharm,
        "_configure",
        lambda *_args, **_kwargs: calls.append("configure") or ops.ActiveStatus("configured"),
    )
    monkeypatch.setattr(
        AlloyCharm,
        "_reconcile_rule_groups",
        lambda *_args, **_kwargs: calls.append("rules"),
    )
    harness = testing.Harness(AlloyCharm)
    harness.begin()
    calls.clear()
    machine = harness.add_relation("machine-observability", "polkadot")
    harness.add_relation_unit(machine, "polkadot/0")

    assert calls == ["configure", "rules"]

    calls.clear()
    harness.update_relation_data(machine, "polkadot", {"payload": json.dumps(_v3_payload())})
    assert calls == ["configure", "rules"]


@pytest.mark.parametrize(
    ("artifact_types", "missing_messages"),
    [
        (("prometheus_alert_rules",), ("send-remote-write relation",)),
        (("loki_alert_rules",), ("send-loki-logs relation",)),
        (
            ("prometheus_alert_rules", "loki_alert_rules"),
            ("send-remote-write relation", "send-loki-logs relation"),
        ),
    ],
)
def test_cached_rules_wait_for_missing_destinations_and_track_join_removal(
    monkeypatch, artifact_types, missing_messages
):
    monkeypatch.setattr(
        AlloyCharm, "_configure", lambda *_args, **_kwargs: ops.ActiveStatus("configured")
    )
    harness = testing.Harness(AlloyCharm)
    harness.set_leader(True)
    harness.begin()
    machine = harness.add_relation("machine-observability", "polkadot")
    artifacts = [
        _rule_artifact(artifact_type, f"rules-{index}", f"Rules {index}")
        for index, artifact_type in enumerate(artifact_types)
    ]

    harness.update_relation_data(
        machine, "polkadot", {"payload": json.dumps(_v3_payload(*artifacts))}
    )

    assert harness.model.unit.status == ops.WaitingStatus(
        f"Waiting for {' and '.join(missing_messages)} for accepted alert rules"
    )
    cached = harness.get_relation_data(machine, harness.charm.app.name)["_alloy_vm_rule_state_v1"]

    destinations = []
    for artifact_type in artifact_types:
        relation_name = (
            "send-remote-write" if artifact_type == "prometheus_alert_rules" else "send-loki-logs"
        )
        destination = harness.add_relation(relation_name, "backend")
        destinations.append(destination)
        harness.add_relation_unit(destination, "backend/0")

    assert harness.model.unit.status == ops.ActiveStatus("configured")
    for destination in destinations:
        assert (
            len(
                json.loads(
                    harness.get_relation_data(destination, harness.charm.app.name)["alert_rules"]
                )["groups"]
            )
            == 1
        )

    harness.remove_relation(destinations[0])

    assert isinstance(harness.model.unit.status, ops.WaitingStatus)
    assert missing_messages[0] in harness.model.unit.status.message
    assert (
        harness.get_relation_data(machine, harness.charm.app.name)["_alloy_vm_rule_state_v1"]
        == cached
    )


def test_machine_rules_publish_to_matching_standard_backends_and_withdraw(monkeypatch):
    import charm as charm_module

    monkeypatch.setattr(
        charm_module.AlloyCharm, "_configure", lambda *args, **kwargs: ops.ActiveStatus()
    )
    harness = testing.Harness(AlloyCharm)
    harness.set_leader(True)
    harness.begin()
    machine = harness.add_relation("machine-observability", "polkadot")
    prometheus = harness.add_relation("send-remote-write", "mimir")
    loki = harness.add_relation("send-loki-logs", "loki")
    grafana_cloud = harness.add_relation("grafana-cloud-config", "grafana-cloud")

    harness.update_relation_data(
        machine,
        "polkadot",
        {
            "payload": json.dumps(
                _v3_payload(
                    _rule_artifact("prometheus_alert_rules", "metrics", "Metrics"),
                    _rule_artifact("loki_alert_rules", "logs", "Logs"),
                )
            )
        },
    )

    prometheus_groups = json.loads(
        harness.get_relation_data(prometheus, harness.charm.app.name)["alert_rules"]
    )["groups"]
    loki_groups = json.loads(
        harness.get_relation_data(loki, harness.charm.app.name)["alert_rules"]
    )["groups"]
    assert [group["name"] for group in prometheus_groups] == sorted(
        group["name"] for group in prometheus_groups
    )
    assert len(prometheus_groups) == 1
    assert len(loki_groups) == 1
    grafana_data = harness.get_relation_data(grafana_cloud, harness.charm.app.name)
    assert "alert_rules" not in grafana_data

    harness.remove_relation(machine)

    assert json.loads(
        harness.get_relation_data(prometheus, harness.charm.app.name)["alert_rules"]
    ) == {"groups": []}
    assert json.loads(harness.get_relation_data(loki, harness.charm.app.name)["alert_rules"]) == {
        "groups": []
    }


def test_bad_artifact_does_not_block_valid_sibling_and_fixed_payload_converges(
    monkeypatch, caplog
):
    import charm as charm_module

    monkeypatch.setattr(
        charm_module.AlloyCharm, "_configure", lambda *args, **kwargs: ops.ActiveStatus()
    )
    harness = testing.Harness(AlloyCharm)
    harness.set_leader(True)
    harness.begin()
    machine = harness.add_relation("machine-observability", "polkadot")
    prometheus = harness.add_relation("send-remote-write", "mimir")
    good = _rule_artifact("prometheus_alert_rules", "good", "Good v1")
    sibling = _rule_artifact("prometheus_alert_rules", "sibling", "Sibling v1")

    harness.update_relation_data(
        machine,
        "polkadot",
        {"payload": json.dumps(_v3_payload(good, sibling))},
    )

    bad = _rule_artifact(
        "prometheus_alert_rules", "good", "Sensitive replacement", checksum_valid=False
    )
    updated_sibling = _rule_artifact("prometheus_alert_rules", "sibling", "Sibling v2")

    harness.update_relation_data(
        machine,
        "polkadot",
        {"payload": json.dumps(_v3_payload(bad, updated_sibling))},
    )

    groups = json.loads(
        harness.get_relation_data(prometheus, harness.charm.app.name)["alert_rules"]
    )["groups"]
    assert len(groups) == 2
    assert any(group["name"].endswith("good-Good-v1") for group in groups)
    assert any(group["name"].endswith("sibling-Sibling-v2") for group in groups)
    assert "prometheus_alert_rules/good: checksum" in caplog.text
    assert "Sensitive replacement" not in caplog.text

    harness.update_relation_data(
        machine,
        "polkadot",
        {
            "payload": json.dumps(
                _v3_payload(_rule_artifact("prometheus_alert_rules", "good", "Good v2"))
            )
        },
    )

    groups = json.loads(
        harness.get_relation_data(prometheus, harness.charm.app.name)["alert_rules"]
    )["groups"]
    assert len(groups) == 1
    assert groups[0]["name"].endswith("good-Good-v2")
    assert "checksum" not in harness.charm.unit.status.message


def test_unidentifiable_artifact_preserves_unknown_lkg_while_valid_sibling_updates(
    monkeypatch,
):
    import charm as charm_module

    monkeypatch.setattr(
        charm_module.AlloyCharm, "_configure", lambda *args, **kwargs: ops.ActiveStatus()
    )
    harness = testing.Harness(AlloyCharm)
    harness.set_leader(True)
    harness.begin()
    machine = harness.add_relation("machine-observability", "polkadot")
    prometheus = harness.add_relation("send-remote-write", "mimir")
    harness.update_relation_data(
        machine,
        "polkadot",
        {
            "payload": json.dumps(
                _v3_payload(
                    _rule_artifact("prometheus_alert_rules", "owned", "OWNED-V1"),
                    _rule_artifact("prometheus_alert_rules", "sibling", "SIBLING-V1"),
                )
            )
        },
    )

    harness.update_relation_data(
        machine,
        "polkadot",
        {
            "payload": json.dumps(
                _v3_payload(
                    _rule_artifact("prometheus_alert_rules", "sibling", "SIBLING-V2"),
                    None,
                )
            )
        },
    )

    groups = json.loads(
        harness.get_relation_data(prometheus, harness.charm.app.name)["alert_rules"]
    )["groups"]
    assert any(group["name"].endswith("owned-OWNED-V1") for group in groups)
    assert any(group["name"].endswith("sibling-SIBLING-V2") for group in groups)

    harness.update_relation_data(
        machine,
        "polkadot",
        {
            "payload": json.dumps(
                _v3_payload(_rule_artifact("prometheus_alert_rules", "sibling", "SIBLING-V3"))
            )
        },
    )
    groups = json.loads(
        harness.get_relation_data(prometheus, harness.charm.app.name)["alert_rules"]
    )["groups"]
    assert not any(group["name"].endswith("owned-OWNED-V1") for group in groups)
    assert any(group["name"].endswith("sibling-SIBLING-V3") for group in groups)


@pytest.mark.parametrize(
    ("unsafe_type", "unsafe_id"),
    [
        ("unknown_alert_rules", "owned"),
        ("prometheus_alert_rules", "INVALID ID"),
    ],
)
def test_unsafe_artifact_identity_preserves_unknown_lkg_while_valid_sibling_updates(
    monkeypatch,
    unsafe_type,
    unsafe_id,
):
    import charm as charm_module

    monkeypatch.setattr(
        charm_module.AlloyCharm, "_configure", lambda *args, **kwargs: ops.ActiveStatus()
    )
    harness = testing.Harness(AlloyCharm)
    harness.set_leader(True)
    harness.begin()
    machine = harness.add_relation("machine-observability", "polkadot")
    prometheus = harness.add_relation("send-remote-write", "mimir")
    harness.update_relation_data(
        machine,
        "polkadot",
        {
            "payload": json.dumps(
                _v3_payload(
                    _rule_artifact("prometheus_alert_rules", "owned", "OWNED-V1"),
                    _rule_artifact("prometheus_alert_rules", "sibling", "SIBLING-V1"),
                )
            )
        },
    )
    unsafe = _rule_artifact("prometheus_alert_rules", "placeholder", "UNSAFE")
    unsafe["artifact_type"] = unsafe_type
    unsafe["artifact_id"] = unsafe_id

    harness.update_relation_data(
        machine,
        "polkadot",
        {
            "payload": json.dumps(
                _v3_payload(
                    _rule_artifact("prometheus_alert_rules", "sibling", "SIBLING-V2"),
                    unsafe,
                )
            )
        },
    )

    groups = json.loads(
        harness.get_relation_data(prometheus, harness.charm.app.name)["alert_rules"]
    )["groups"]
    assert any(group["name"].endswith("owned-OWNED-V1") for group in groups)
    assert any(group["name"].endswith("sibling-SIBLING-V2") for group in groups)

    harness.update_relation_data(
        machine,
        "polkadot",
        {
            "payload": json.dumps(
                _v3_payload(_rule_artifact("prometheus_alert_rules", "sibling", "SIBLING-V3"))
            )
        },
    )
    groups = json.loads(
        harness.get_relation_data(prometheus, harness.charm.app.name)["alert_rules"]
    )["groups"]
    assert not any(group["name"].endswith("owned-OWNED-V1") for group in groups)
    assert any(group["name"].endswith("sibling-SIBLING-V3") for group in groups)


def test_duplicate_identifiable_artifacts_retain_whole_relation_lkg(monkeypatch):
    import charm as charm_module

    monkeypatch.setattr(
        charm_module.AlloyCharm, "_configure", lambda *args, **kwargs: ops.ActiveStatus()
    )
    harness = testing.Harness(AlloyCharm)
    harness.set_leader(True)
    harness.begin()
    machine = harness.add_relation("machine-observability", "polkadot")
    prometheus = harness.add_relation("send-remote-write", "mimir")
    harness.update_relation_data(
        machine,
        "polkadot",
        {
            "payload": json.dumps(
                _v3_payload(_rule_artifact("prometheus_alert_rules", "owned", "LAST-GOOD"))
            )
        },
    )

    harness.update_relation_data(
        machine,
        "polkadot",
        {
            "payload": json.dumps(
                _v3_payload(
                    _rule_artifact("prometheus_alert_rules", "owned", "AMBIGUOUS-A"),
                    _rule_artifact("prometheus_alert_rules", "owned", "AMBIGUOUS-B"),
                )
            )
        },
    )

    groups = json.loads(
        harness.get_relation_data(prometheus, harness.charm.app.name)["alert_rules"]
    )["groups"]
    assert len(groups) == 1
    assert groups[0]["name"].endswith("owned-LAST-GOOD")


def test_semantically_invalid_rule_retains_artifact_lkg(monkeypatch):
    import charm as charm_module

    monkeypatch.setattr(
        charm_module.AlloyCharm, "_configure", lambda *args, **kwargs: ops.ActiveStatus()
    )
    harness = testing.Harness(AlloyCharm)
    harness.set_leader(True)
    harness.begin()
    machine = harness.add_relation("machine-observability", "polkadot")
    prometheus = harness.add_relation("send-remote-write", "mimir")
    harness.update_relation_data(
        machine,
        "polkadot",
        {
            "payload": json.dumps(
                _v3_payload(_rule_artifact("prometheus_alert_rules", "good", "LAST-GOOD"))
            )
        },
    )
    invalid = encode_artifact(
        artifact_type="prometheus_alert_rules",
        artifact_id="good",
        content=json.dumps(
            {
                "groups": [
                    {
                        "name": "INVALID",
                        "rules": [{"alert": "Down", "record": "down", "expr": "up"}],
                    }
                ]
            }
        ).encode(),
    ).model_dump()

    harness.update_relation_data(
        machine, "polkadot", {"payload": json.dumps(_v3_payload(invalid))}
    )

    groups = json.loads(
        harness.get_relation_data(prometheus, harness.charm.app.name)["alert_rules"]
    )["groups"]
    assert len(groups) == 1
    assert groups[0]["name"].endswith("good-LAST-GOOD")


@pytest.mark.parametrize("artifact_type", ["prometheus_alert_rules", "loki_alert_rules"])
def test_backend_invalid_artifact_retains_only_its_lkg_and_valid_sibling_updates(
    monkeypatch, caplog, artifact_type
):
    import charm as charm_module

    monkeypatch.setattr(
        charm_module.AlloyCharm, "_configure", lambda *args, **kwargs: ops.ActiveStatus()
    )

    def validate(_self, _kind, groups):
        return all(
            "INVALID-BACKEND" not in rule["expr"] for group in groups for rule in group["rules"]
        )

    monkeypatch.setattr(charm_module.AlloyCharm, "_validate_artifact_rules", validate)
    harness = testing.Harness(AlloyCharm)
    harness.set_leader(True)
    harness.begin()
    machine = harness.add_relation("machine-observability", "polkadot")
    backend_relation = harness.add_relation(
        "send-remote-write" if artifact_type == "prometheus_alert_rules" else "send-loki-logs",
        "backend",
    )
    harness.update_relation_data(
        machine,
        "polkadot",
        {
            "payload": json.dumps(
                _v3_payload(
                    _rule_artifact(artifact_type, "owned", "LAST-GOOD"),
                    _rule_artifact(artifact_type, "sibling", "SIBLING-V1"),
                )
            )
        },
    )
    invalid = encode_artifact(
        artifact_type=artifact_type,
        artifact_id="owned",
        content=json.dumps(
            {
                "groups": [
                    {"name": "INVALID", "rules": [{"alert": "Down", "expr": "INVALID-BACKEND"}]}
                ]
            }
        ).encode(),
    ).model_dump()

    harness.update_relation_data(
        machine,
        "polkadot",
        {
            "payload": json.dumps(
                _v3_payload(invalid, _rule_artifact(artifact_type, "sibling", "SIBLING-V2"))
            )
        },
    )

    groups = json.loads(
        harness.get_relation_data(backend_relation, harness.charm.app.name)["alert_rules"]
    )["groups"]
    assert any(group["name"].endswith("owned-LAST-GOOD") for group in groups)
    assert any(group["name"].endswith("sibling-SIBLING-V2") for group in groups)
    assert f"{artifact_type}/owned: validation" in caplog.text
    assert "INVALID-BACKEND" not in caplog.text


def test_malformed_outer_payload_retains_rules_and_v2_withdraws_them(monkeypatch, caplog):
    import charm as charm_module

    monkeypatch.setattr(
        charm_module.AlloyCharm, "_configure", lambda *args, **kwargs: ops.ActiveStatus()
    )
    harness = testing.Harness(AlloyCharm)
    harness.set_leader(True)
    harness.begin()
    machine = harness.add_relation("machine-observability", "polkadot")
    prometheus = harness.add_relation("send-remote-write", "mimir")
    rule = _rule_artifact("prometheus_alert_rules", "good", "Good")
    harness.update_relation_data(machine, "polkadot", {"payload": json.dumps(_v3_payload(rule))})

    harness.update_relation_data(machine, "polkadot", {"payload": "not-json-secret"})

    assert (
        len(
            json.loads(
                harness.get_relation_data(prometheus, harness.charm.app.name)["alert_rules"]
            )["groups"]
        )
        == 1
    )
    assert "not-json-secret" not in caplog.text

    harness.update_relation_data(
        machine,
        "polkadot",
        {"payload": json.dumps({"schema_version": 99, "artifacts": []})},
    )
    assert (
        len(
            json.loads(
                harness.get_relation_data(prometheus, harness.charm.app.name)["alert_rules"]
            )["groups"]
        )
        == 1
    )

    harness.update_relation_data(
        machine,
        "polkadot",
        {"payload": json.dumps({"schema_version": 2, "artifacts": [rule]})},
    )
    assert (
        len(
            json.loads(
                harness.get_relation_data(prometheus, harness.charm.app.name)["alert_rules"]
            )["groups"]
        )
        == 1
    )

    harness.update_relation_data(
        machine,
        "polkadot",
        {
            "payload": json.dumps(
                {"schema_version": 2, "source_topology": _v3_payload()["source_topology"]}
            )
        },
    )
    assert json.loads(
        harness.get_relation_data(prometheus, harness.charm.app.name)["alert_rules"]
    ) == {"groups": []}


@pytest.mark.parametrize("invalid_kind", ["oversized", "deep"])
def test_bounded_invalid_payload_retains_telemetry_config_and_relation_rule_lkg(
    invalid_kind,
):
    writes: list[str] = []
    with ExitStack() as stack:
        for manager in _patch_runtime():
            stack.enter_context(manager)
        stack.enter_context(
            patch(
                "charm.PrometheusRemoteWriteConsumer.endpoints",
                new_callable=PropertyMock,
                return_value=[{"url": "http://mimir:9009/api/v1/push"}],
            )
        )
        stack.enter_context(
            patch(
                "charm.alloy.write_config_text",
                side_effect=lambda config_text, **_: writes.append(config_text),
            )
        )
        harness = testing.Harness(AlloyCharm)
        harness.set_leader(True)
        harness.begin()
        machine = harness.add_relation("machine-observability", "polkadot")
        prometheus = harness.add_relation("send-remote-write", "mimir")
        valid = _v3_payload(_rule_artifact("prometheus_alert_rules", "good", "LAST-GOOD"))
        valid["systemd_units"] = ["polkadot.service"]
        harness.update_relation_data(machine, "polkadot", {"payload": json.dumps(valid)})
        writes_after_valid = list(writes)
        assert writes_after_valid
        assert "polkadot.service" in writes_after_valid[-1]

        if invalid_kind == "oversized":
            raw_invalid = json.dumps(_v3_payload())
            raw_invalid += " " * (
                MAX_SERIALIZED_PAYLOAD_BYTES + 1 - len(raw_invalid.encode("utf-8"))
            )
        else:
            raw_invalid = '{"schema_version":3,"artifacts":' + "[" * 10_000 + "]" * 10_000 + "}"
        harness.update_relation_data(machine, "polkadot", {"payload": raw_invalid})

    assert writes == writes_after_valid
    groups = json.loads(
        harness.get_relation_data(prometheus, harness.charm.app.name)["alert_rules"]
    )["groups"]
    assert len(groups) == 1
    assert groups[0]["name"].endswith("good-LAST-GOOD")
    assert harness.charm.unit.status.name == "blocked"


@pytest.mark.parametrize(
    "invalid_payload",
    [
        "not-json",
        json.dumps({"schema_version": 99, "artifacts": []}),
    ],
)
def test_shared_relation_cache_survives_new_leader_with_invalid_current_payload(
    monkeypatch, invalid_payload
):
    import charm as charm_module

    monkeypatch.setattr(
        charm_module.AlloyCharm, "_configure", lambda *args, **kwargs: ops.ActiveStatus()
    )
    first = testing.Harness(AlloyCharm)
    first.set_leader(True)
    first.begin()
    machine = first.add_relation("machine-observability", "polkadot")
    first.add_relation("send-remote-write", "mimir")
    first.update_relation_data(
        machine,
        "polkadot",
        {
            "payload": json.dumps(
                _v3_payload(_rule_artifact("prometheus_alert_rules", "good", "LAST-GOOD"))
            )
        },
    )
    shared_cache = first.get_relation_data(machine, first.charm.app.name)[
        charm_module.RULE_CACHE_KEY
    ]

    replacement = testing.Harness(AlloyCharm)
    replacement.begin()
    replacement_machine = replacement.add_relation("machine-observability", "polkadot")
    prometheus = replacement.add_relation("send-remote-write", "mimir")
    replacement.update_relation_data(
        replacement_machine,
        replacement.charm.app.name,
        {charm_module.RULE_CACHE_KEY: shared_cache},
    )
    replacement.update_relation_data(
        replacement_machine,
        "polkadot",
        {"payload": invalid_payload},
    )

    replacement.set_leader(True)

    groups = json.loads(
        replacement.get_relation_data(prometheus, replacement.charm.app.name)["alert_rules"]
    )["groups"]
    assert len(groups) == 1
    assert groups[0]["name"].endswith("good-LAST-GOOD")


def test_corrupt_shared_relation_cache_fails_closed_without_logging_content(monkeypatch, caplog):
    import charm as charm_module

    monkeypatch.setattr(
        charm_module.AlloyCharm, "_configure", lambda *args, **kwargs: ops.ActiveStatus()
    )
    attacker = {
        "prometheus_alert_rules/owned": {
            "backend": "prometheus",
            "ownership": "principal-uuid/polkadot/prometheus_alert_rules/owned",
            "groups": [
                {
                    "name": "apparently-valid",
                    "rules": [
                        {
                            "alert": "Down",
                            "expr": "up == 0",
                            "UNIQUE-CACHE-MARKER": {"attacker": "structure"},
                        }
                    ],
                }
            ],
        }
    }
    encoded = base64.b64encode(
        zlib.compress(json.dumps(attacker, separators=(",", ":")).encode())
    ).decode()
    harness = testing.Harness(AlloyCharm)
    harness.begin()
    machine = harness.add_relation("machine-observability", "polkadot")
    prometheus = harness.add_relation("send-remote-write", "mimir")
    harness.update_relation_data(
        machine,
        harness.charm.app.name,
        {charm_module.RULE_CACHE_KEY: f"v1:{encoded}"},
    )
    harness.update_relation_data(machine, "polkadot", {"payload": "invalid-current-secret"})
    caplog.clear()

    harness.set_leader(True)

    assert json.loads(
        harness.get_relation_data(prometheus, harness.charm.app.name)["alert_rules"]
    ) == {"groups": []}
    assert "UNIQUE-CACHE-MARKER" not in caplog.text
    assert "invalid-current-secret" not in caplog.text


def test_backend_validated_shared_cache_loads_without_subprocess_validation(monkeypatch, caplog):
    import charm as charm_module

    monkeypatch.setattr(
        charm_module.AlloyCharm, "_configure", lambda *args, **kwargs: ops.ActiveStatus()
    )
    monkeypatch.setattr(
        charm_module.AlloyCharm,
        "_validate_artifact_rules",
        lambda *_args: (_ for _ in ()).throw(AssertionError("cache must not invoke cos-tool")),
    )
    state = {
        "prometheus_alert_rules/owned": {
            "backend": "prometheus",
            "ownership": "principal-uuid/polkadot/prometheus_alert_rules/owned",
            "groups": [{"name": "rules", "rules": [{"alert": "Down", "expr": "up == 0"}]}],
        }
    }
    harness = testing.Harness(AlloyCharm)
    harness.begin()
    machine = harness.add_relation("machine-observability", "polkadot")
    prometheus = harness.add_relation("send-remote-write", "mimir")
    harness.update_relation_data(
        machine,
        harness.charm.app.name,
        {charm_module.RULE_CACHE_KEY: harness.charm._encode_rule_cache(state)},
    )
    harness.update_relation_data(machine, "polkadot", {"payload": "invalid-current-secret"})
    caplog.clear()

    harness.set_leader(True)

    groups = json.loads(
        harness.get_relation_data(prometheus, harness.charm.app.name)["alert_rules"]
    )["groups"]
    assert groups == state["prometheus_alert_rules/owned"]["groups"]
    assert "cache-validation" not in caplog.text
    assert "invalid-current-secret" not in caplog.text


def test_multiple_machine_relations_share_and_reset_one_validator_budget(monkeypatch):
    class BudgetValidator:
        def __init__(self):
            self.calls_per_reconcile: list[int] = []

        def begin_reconcile(self):
            self.calls_per_reconcile.append(0)

        def __call__(self, _artifact_type, _groups):
            if self.calls_per_reconcile[-1] >= 32:
                return False
            self.calls_per_reconcile[-1] += 1
            return True

    monkeypatch.setattr(
        AlloyCharm, "_configure", lambda *_args, **_kwargs: ops.ActiveStatus("configured")
    )
    monkeypatch.setattr(
        AlloyCharm,
        "_validate_artifact_rules",
        lambda self, artifact_type, groups: self._rule_validator(artifact_type, groups),
    )
    harness = testing.Harness(AlloyCharm)
    harness.set_leader(True)
    harness.begin()
    first = harness.add_relation("machine-observability", "first")
    second = harness.add_relation("machine-observability", "second")
    first_relation = harness.model.get_relation("machine-observability", first)
    second_relation = harness.model.get_relation("machine-observability", second)
    assert first_relation is not None
    assert second_relation is not None
    first_payload = _v3_payload(
        *[
            _rule_artifact("prometheus_alert_rules", f"first-{index:02d}", f"First {index}")
            for index in range(20)
        ]
    )
    second_payload = _v3_payload(
        *[
            _rule_artifact("prometheus_alert_rules", f"second-{index:02d}", f"Second {index}")
            for index in range(20)
        ]
    )
    first_payload["source_topology"]["application"] = "first"
    second_payload["source_topology"]["application"] = "second"
    first_relation.data[first_relation.app]["payload"] = json.dumps(first_payload)
    second_relation.data[second_relation.app]["payload"] = json.dumps(second_payload)
    validator = BudgetValidator()
    cast(Any, harness.charm)._rule_validator = validator

    harness.charm._reconcile_rule_groups(cast(ops.EventBase, SimpleNamespace()))
    harness.charm._reconcile_rule_groups(cast(ops.EventBase, SimpleNamespace()))

    assert validator.calls_per_reconcile == [32, 32]


def test_artifact_beyond_limit_retains_its_lkg(monkeypatch):
    monkeypatch.setattr(
        AlloyCharm, "_configure", lambda *_args, **_kwargs: ops.ActiveStatus("configured")
    )
    harness = testing.Harness(AlloyCharm)
    harness.set_leader(True)
    harness.begin()
    machine = harness.add_relation("machine-observability", "polkadot")
    prometheus = harness.add_relation("send-remote-write", "mimir")
    harness.update_relation_data(
        machine,
        "polkadot",
        {
            "payload": json.dumps(
                _v3_payload(_rule_artifact("prometheus_alert_rules", "z-last-good", "LAST-GOOD"))
            )
        },
    )
    candidates = [
        _rule_artifact("prometheus_alert_rules", f"a-{index:02d}", f"Candidate {index}")
        for index in range(32)
    ]
    candidates.append(_rule_artifact("prometheus_alert_rules", "z-last-good", "REPLACEMENT"))

    harness.update_relation_data(
        machine, "polkadot", {"payload": json.dumps(_v3_payload(*candidates))}
    )

    groups = json.loads(
        harness.get_relation_data(prometheus, harness.charm.app.name)["alert_rules"]
    )["groups"]
    assert len(groups) == 33
    assert any(group["name"].endswith("z-last-good-LAST-GOOD") for group in groups)
    assert not any(group["name"].endswith("z-last-good-REPLACEMENT") for group in groups)


def test_later_relation_retains_lkg_after_shared_validator_budget_is_exhausted(monkeypatch):
    class BudgetValidator:
        def begin_reconcile(self):
            self.remaining = 32

        def __call__(self, _artifact_type, _groups):
            if self.remaining == 0:
                return False
            self.remaining -= 1
            return True

    monkeypatch.setattr(
        AlloyCharm, "_configure", lambda *_args, **_kwargs: ops.ActiveStatus("configured")
    )
    monkeypatch.setattr(
        AlloyCharm,
        "_validate_artifact_rules",
        lambda self, artifact_type, groups: self._rule_validator(artifact_type, groups),
    )
    harness = testing.Harness(AlloyCharm)
    harness.set_leader(True)
    harness.begin()
    first = harness.add_relation("machine-observability", "first")
    later = harness.add_relation("machine-observability", "later")
    prometheus = harness.add_relation("send-remote-write", "mimir")
    first_relation = harness.model.get_relation("machine-observability", first)
    later_relation = harness.model.get_relation("machine-observability", later)
    assert first_relation is not None
    assert later_relation is not None
    first_payload = _v3_payload(
        *[
            _rule_artifact("prometheus_alert_rules", f"first-{index:02d}", f"First {index}")
            for index in range(32)
        ]
    )
    first_payload["source_topology"]["application"] = "first"
    later_payload = _v3_payload(_rule_artifact("prometheus_alert_rules", "owned", "REPLACEMENT"))
    later_payload["source_topology"]["application"] = "later"
    first_relation.data[first_relation.app]["payload"] = json.dumps(first_payload)
    later_relation.data[later_relation.app]["payload"] = json.dumps(later_payload)
    lkg = {
        "prometheus_alert_rules/owned": {
            "backend": "prometheus",
            "ownership": "principal-uuid/later/prometheus_alert_rules/owned",
            "groups": [{"name": "last-good", "rules": [{"alert": "Down", "expr": "up == 0"}]}],
        }
    }
    later_relation.data[harness.charm.app]["_alloy_vm_rule_state_v1"] = (
        harness.charm._encode_rule_cache(lkg)
    )
    cast(Any, harness.charm)._rule_validator = BudgetValidator()

    harness.charm._reconcile_rule_groups(cast(ops.EventBase, SimpleNamespace()))

    groups = json.loads(
        harness.get_relation_data(prometheus, harness.charm.app.name)["alert_rules"]
    )["groups"]
    assert len(groups) == 33
    assert any(group["name"] == "last-good" for group in groups)
    assert not any(group["name"].endswith("owned-REPLACEMENT") for group in groups)


@pytest.mark.parametrize(
    "cache_json",
    [
        "[" * 10_000 + "0" + "]" * 10_000,
        json.dumps({str(index): {} for index in range(20_000)}, separators=(",", ":")),
        json.dumps({"nested": [{"children": [{}] * 1000}] * 100}, separators=(",", ":")),
    ],
    ids=("deep", "broad", "many-nodes"),
)
def test_adversarial_cache_structure_fails_closed_and_reconcile_continues(
    monkeypatch, caplog, cache_json
):
    import charm as charm_module

    monkeypatch.setattr(
        charm_module.AlloyCharm, "_configure", lambda *args, **kwargs: ops.ActiveStatus()
    )
    encoded = base64.b64encode(zlib.compress(cache_json.encode())).decode()
    harness = testing.Harness(AlloyCharm)
    harness.begin()
    machine = harness.add_relation("machine-observability", "polkadot")
    prometheus = harness.add_relation("send-remote-write", "mimir")
    harness.update_relation_data(
        machine,
        harness.charm.app.name,
        {charm_module.RULE_CACHE_KEY: f"v1:{encoded}"},
    )
    harness.update_relation_data(machine, "polkadot", {"payload": "invalid-current-secret"})

    harness.set_leader(True)

    assert json.loads(
        harness.get_relation_data(prometheus, harness.charm.app.name)["alert_rules"]
    ) == {"groups": []}
    assert "cache-validation" in caplog.text
    assert "invalid-current-secret" not in caplog.text


def test_relation_cache_size_failure_retains_prior_durable_state(monkeypatch, caplog):
    import charm as charm_module

    monkeypatch.setattr(
        charm_module.AlloyCharm, "_configure", lambda *args, **kwargs: ops.ActiveStatus()
    )
    harness = testing.Harness(AlloyCharm)
    harness.set_leader(True)
    harness.begin()
    machine = harness.add_relation("machine-observability", "polkadot")
    prometheus = harness.add_relation("send-remote-write", "mimir")
    harness.update_relation_data(
        machine,
        "polkadot",
        {
            "payload": json.dumps(
                _v3_payload(_rule_artifact("prometheus_alert_rules", "good", "LAST-GOOD"))
            )
        },
    )
    monkeypatch.setattr(charm_module, "RULE_CACHE_VALUE_LIMIT", 20)

    harness.update_relation_data(
        machine,
        "polkadot",
        {
            "payload": json.dumps(
                _v3_payload(_rule_artifact("prometheus_alert_rules", "good", "TOO-LARGE"))
            )
        },
    )

    groups = json.loads(
        harness.get_relation_data(prometheus, harness.charm.app.name)["alert_rules"]
    )["groups"]
    assert groups[0]["name"].endswith("good-LAST-GOOD")
    assert "TOO-LARGE" not in caplog.text
    assert "cache-size" in caplog.text


def test_relation_cache_decoded_size_failure_retains_prior_durable_state(monkeypatch, caplog):
    import charm as charm_module

    monkeypatch.setattr(
        charm_module.AlloyCharm, "_configure", lambda *args, **kwargs: ops.ActiveStatus()
    )
    harness = testing.Harness(AlloyCharm)
    harness.set_leader(True)
    harness.begin()
    machine = harness.add_relation("machine-observability", "polkadot")
    prometheus = harness.add_relation("send-remote-write", "mimir")
    harness.update_relation_data(
        machine,
        "polkadot",
        {
            "payload": json.dumps(
                _v3_payload(_rule_artifact("prometheus_alert_rules", "good", "LAST-GOOD"))
            )
        },
    )
    monkeypatch.setattr(charm_module, "RULE_CACHE_DECODED_LIMIT", 200)

    harness.update_relation_data(
        machine,
        "polkadot",
        {
            "payload": json.dumps(
                _v3_payload(_rule_artifact("prometheus_alert_rules", "good", "COMPRESSIBLE"))
            )
        },
    )

    groups = json.loads(
        harness.get_relation_data(prometheus, harness.charm.app.name)["alert_rules"]
    )["groups"]
    assert groups[0]["name"].endswith("good-LAST-GOOD")
    assert "COMPRESSIBLE" not in caplog.text
    assert "cache-size" in caplog.text


def test_v3_empty_artifacts_without_topology_withdraws_relation_lkg(monkeypatch):
    import charm as charm_module

    monkeypatch.setattr(
        charm_module.AlloyCharm, "_configure", lambda *args, **kwargs: ops.ActiveStatus()
    )
    harness = testing.Harness(AlloyCharm)
    harness.set_leader(True)
    harness.begin()
    machine = harness.add_relation("machine-observability", "polkadot")
    prometheus = harness.add_relation("send-remote-write", "mimir")
    harness.update_relation_data(
        machine,
        "polkadot",
        {
            "payload": json.dumps(
                _v3_payload(_rule_artifact("prometheus_alert_rules", "good", "LAST-GOOD"))
            )
        },
    )

    harness.update_relation_data(
        machine,
        "polkadot",
        {"payload": json.dumps({"schema_version": 3, "artifacts": []})},
    )

    assert json.loads(
        harness.get_relation_data(prometheus, harness.charm.app.name)["alert_rules"]
    ) == {"groups": []}


def test_downstream_value_overflow_retains_prior_durable_state(monkeypatch, caplog):
    import charm as charm_module

    monkeypatch.setattr(
        charm_module.AlloyCharm, "_configure", lambda *args, **kwargs: ops.ActiveStatus()
    )
    harness = testing.Harness(AlloyCharm)
    harness.set_leader(True)
    harness.begin()
    machine = harness.add_relation("machine-observability", "polkadot")
    prometheus = harness.add_relation("send-remote-write", "mimir")
    harness.update_relation_data(
        machine,
        "polkadot",
        {
            "payload": json.dumps(
                _v3_payload(_rule_artifact("prometheus_alert_rules", "good", "LAST-GOOD"))
            )
        },
    )
    prior_payload = harness.get_relation_data(prometheus, harness.charm.app.name)["alert_rules"]
    prior_cache = harness.get_relation_data(machine, harness.charm.app.name)[
        charm_module.RULE_CACHE_KEY
    ]
    monkeypatch.setattr(
        charm_module, "RULE_PUBLICATION_VALUE_LIMIT", len(prior_payload.encode()) + 50
    )

    harness.update_relation_data(
        machine,
        "polkadot",
        {
            "payload": json.dumps(
                _v3_payload(_rule_artifact("prometheus_alert_rules", "good", "X" * 2000))
            )
        },
    )

    assert (
        harness.get_relation_data(prometheus, harness.charm.app.name)["alert_rules"]
        == prior_payload
    )
    assert (
        harness.get_relation_data(machine, harness.charm.app.name)[charm_module.RULE_CACHE_KEY]
        == prior_cache
    )
    assert "publish-size" in caplog.text
    assert "X" * 2000 not in caplog.text


def test_multi_relation_aggregate_overflow_admits_states_deterministically(monkeypatch):
    import charm as charm_module

    monkeypatch.setattr(
        charm_module.AlloyCharm, "_configure", lambda *args, **kwargs: ops.ActiveStatus()
    )
    harness = testing.Harness(AlloyCharm)
    harness.set_leader(True)
    harness.begin()
    first = harness.add_relation("machine-observability", "polkadot")
    second = harness.add_relation("machine-observability", "kusama")
    prometheus = harness.add_relation("send-remote-write", "mimir")
    harness.update_relation_data(
        first,
        "polkadot",
        {
            "payload": json.dumps(
                _v3_payload(_rule_artifact("prometheus_alert_rules", "first", "FIRST"))
            )
        },
    )
    first_payload = harness.get_relation_data(prometheus, harness.charm.app.name)["alert_rules"]
    monkeypatch.setattr(
        charm_module, "RULE_PUBLICATION_VALUE_LIMIT", len(first_payload.encode()) + 50
    )
    second_payload = _v3_payload(_rule_artifact("prometheus_alert_rules", "second", "SECOND"))
    second_payload["source_topology"] = {
        **second_payload["source_topology"],
        "model_uuid": "second-uuid",
        "application": "kusama",
        "unit": "kusama/0",
    }

    harness.update_relation_data(second, "kusama", {"payload": json.dumps(second_payload)})

    groups = json.loads(
        harness.get_relation_data(prometheus, harness.charm.app.name)["alert_rules"]
    )["groups"]
    assert len(groups) == 1
    assert groups[0]["name"].endswith("first-FIRST")
    second_cache = harness.get_relation_data(second, harness.charm.app.name)[
        charm_module.RULE_CACHE_KEY
    ]
    assert harness.charm._decode_rule_cache(second_cache) == {}


def test_duplicate_ownership_lower_relation_wins_without_losing_unrelated_rules(
    monkeypatch, caplog
):
    import charm as charm_module

    monkeypatch.setattr(
        charm_module.AlloyCharm, "_configure", lambda *args, **kwargs: ops.ActiveStatus()
    )
    harness = testing.Harness(AlloyCharm)
    harness.set_leader(True)
    harness.begin()
    first = harness.add_relation("machine-observability", "polkadot")
    second = harness.add_relation("machine-observability", "kusama")
    prometheus = harness.add_relation("send-remote-write", "mimir")
    harness.update_relation_data(
        first,
        "polkadot",
        {
            "payload": json.dumps(
                _v3_payload(_rule_artifact("prometheus_alert_rules", "shared", "FIRST"))
            )
        },
    )
    harness.update_relation_data(
        second,
        "kusama",
        {
            "payload": json.dumps(
                _v3_payload(
                    _rule_artifact("prometheus_alert_rules", "shared", "SECOND"),
                    _rule_artifact("prometheus_alert_rules", "unrelated", "UNRELATED"),
                )
            )
        },
    )

    groups = json.loads(
        harness.get_relation_data(prometheus, harness.charm.app.name)["alert_rules"]
    )["groups"]
    assert len(groups) == 2
    assert any(group["name"].endswith("shared-FIRST") for group in groups)
    assert not any(group["name"].endswith("shared-SECOND") for group in groups)
    assert any(group["name"].endswith("unrelated-UNRELATED") for group in groups)
    second_cache = harness.charm._decode_rule_cache(
        harness.get_relation_data(second, harness.charm.app.name)[charm_module.RULE_CACHE_KEY]
    )
    assert isinstance(second_cache, dict)
    assert set(second_cache) == {"prometheus_alert_rules/unrelated"}
    assert f"relation {second}" in caplog.text
    assert "principal-uuid/polkadot/prometheus_alert_rules/shared" in caplog.text

    harness.remove_relation(first)
    harness.update_relation_data(
        second,
        "kusama",
        {
            "payload": json.dumps(
                _v3_payload(
                    _rule_artifact("prometheus_alert_rules", "shared", "SECOND-LATER"),
                    _rule_artifact("prometheus_alert_rules", "unrelated", "UNRELATED"),
                )
            )
        },
    )

    groups = json.loads(
        harness.get_relation_data(prometheus, harness.charm.app.name)["alert_rules"]
    )["groups"]
    assert len(groups) == 2
    assert any(group["name"].endswith("shared-SECOND-LATER") for group in groups)


def test_conflicting_replacement_retains_later_relations_nonconflicting_lkg(monkeypatch):
    import charm as charm_module

    monkeypatch.setattr(
        charm_module.AlloyCharm, "_configure", lambda *args, **kwargs: ops.ActiveStatus()
    )
    harness = testing.Harness(AlloyCharm)
    harness.set_leader(True)
    harness.begin()
    first = harness.add_relation("machine-observability", "polkadot")
    second = harness.add_relation("machine-observability", "kusama")
    prometheus = harness.add_relation("send-remote-write", "mimir")
    harness.update_relation_data(
        first,
        "polkadot",
        {
            "payload": json.dumps(
                _v3_payload(_rule_artifact("prometheus_alert_rules", "shared", "FIRST"))
            )
        },
    )
    unique_payload = _v3_payload(_rule_artifact("prometheus_alert_rules", "shared", "SECOND-LKG"))
    unique_payload["source_topology"] = {
        **unique_payload["source_topology"],
        "model_uuid": "second-uuid",
        "application": "kusama",
        "unit": "kusama/0",
    }
    harness.update_relation_data(second, "kusama", {"payload": json.dumps(unique_payload)})

    harness.update_relation_data(
        second,
        "kusama",
        {
            "payload": json.dumps(
                _v3_payload(_rule_artifact("prometheus_alert_rules", "shared", "COLLISION"))
            )
        },
    )

    groups = json.loads(
        harness.get_relation_data(prometheus, harness.charm.app.name)["alert_rules"]
    )["groups"]
    assert len(groups) == 2
    assert any(group["name"].endswith("shared-FIRST") for group in groups)
    assert any(group["name"].endswith("shared-SECOND-LKG") for group in groups)
    assert not any(group["name"].endswith("shared-COLLISION") for group in groups)


def test_duplicate_final_group_name_makes_candidate_unpublishable():
    group = {"name": "same", "rules": [{"alert": "Down", "expr": "up"}]}
    relation_state = {
        0: {
            "prometheus_alert_rules/one": {
                "backend": "prometheus",
                "ownership": "uuid/app/prometheus_alert_rules/one",
                "groups": [group],
            },
            "prometheus_alert_rules/two": {
                "backend": "prometheus",
                "ownership": "uuid/app/prometheus_alert_rules/two",
                "groups": [group],
            },
        }
    }

    assert not AlloyCharm._rule_state_is_publishable(relation_state)


@pytest.mark.parametrize(
    "topology",
    [
        None,
        {"application": "polkadot"},
        {"model_uuid": "principal-uuid", "application": ""},
    ],
)
def test_invalid_ownership_topology_retains_relation_lkg(monkeypatch, topology):
    import charm as charm_module

    monkeypatch.setattr(
        charm_module.AlloyCharm, "_configure", lambda *args, **kwargs: ops.ActiveStatus()
    )
    harness = testing.Harness(AlloyCharm)
    harness.set_leader(True)
    harness.begin()
    machine = harness.add_relation("machine-observability", "polkadot")
    prometheus = harness.add_relation("send-remote-write", "mimir")
    harness.update_relation_data(
        machine,
        "polkadot",
        {
            "payload": json.dumps(
                _v3_payload(_rule_artifact("prometheus_alert_rules", "good", "LAST-GOOD"))
            )
        },
    )
    invalid = _v3_payload(_rule_artifact("prometheus_alert_rules", "good", "INVALID"))
    if topology is None:
        invalid.pop("source_topology")
    else:
        invalid["source_topology"] = topology

    harness.update_relation_data(machine, "polkadot", {"payload": json.dumps(invalid)})

    groups = json.loads(
        harness.get_relation_data(prometheus, harness.charm.app.name)["alert_rules"]
    )["groups"]
    assert len(groups) == 1
    assert groups[0]["name"].endswith("good-LAST-GOOD")


def test_multiple_machine_relations_are_aggregated_and_removed_independently(monkeypatch):
    import charm as charm_module

    monkeypatch.setattr(
        charm_module.AlloyCharm, "_configure", lambda *args, **kwargs: ops.ActiveStatus()
    )
    harness = testing.Harness(AlloyCharm)
    harness.set_leader(True)
    harness.begin()
    first = harness.add_relation("machine-observability", "polkadot")
    second = harness.add_relation("machine-observability", "kusama")
    prometheus = harness.add_relation("send-remote-write", "mimir")
    assert len(harness.model.relations["machine-observability"]) == 2
    shared = _rule_artifact("prometheus_alert_rules", "node", "Node")
    first_payload = _v3_payload(shared)
    second_payload = _v3_payload(shared)
    second_payload["source_topology"] = {
        **second_payload["source_topology"],
        "model_uuid": "second-uuid",
        "application": "kusama",
        "unit": "kusama/0",
    }
    harness.update_relation_data(first, "polkadot", {"payload": json.dumps(first_payload)})
    harness.update_relation_data(second, "kusama", {"payload": json.dumps(second_payload)})
    harness.add_relation_unit(first, "polkadot/0")

    groups = json.loads(
        harness.get_relation_data(prometheus, harness.charm.app.name)["alert_rules"]
    )["groups"]
    assert len(groups) == 2
    assert any("principal-uuid-polkadot" in group["name"] for group in groups)
    assert any("second-uuid-kusama" in group["name"] for group in groups)

    harness.remove_relation_unit(first, "polkadot/0")

    groups = json.loads(
        harness.get_relation_data(prometheus, harness.charm.app.name)["alert_rules"]
    )["groups"]
    assert len(groups) == 2

    harness.remove_relation(first)

    groups = json.loads(
        harness.get_relation_data(prometheus, harness.charm.app.name)["alert_rules"]
    )["groups"]
    assert len(groups) == 1
    assert "second-uuid-kusama" in groups[0]["name"]


def test_invalid_artifact_error_log_does_not_include_content(monkeypatch, caplog):
    import charm as charm_module

    monkeypatch.setattr(
        charm_module.AlloyCharm, "_configure", lambda *args, **kwargs: ops.ActiveStatus()
    )
    harness = testing.Harness(AlloyCharm)
    harness.set_leader(True)
    harness.begin()
    machine = harness.add_relation("machine-observability", "polkadot")
    harness.add_relation("send-remote-write", "mimir")
    artifact = _rule_artifact("prometheus_alert_rules", "bad-encoding", "Sensitive")
    artifact["encoding"] = "UNIQUE-SECRET-ENCODING"
    artifact["content"] = "UNIQUE-SECRET-ARTIFACT-BODY"

    harness.update_relation_data(
        machine,
        "polkadot",
        {"payload": json.dumps(_v3_payload(artifact))},
    )

    assert "UNIQUE-SECRET-ARTIFACT-BODY" not in caplog.text
    assert "UNIQUE-SECRET-ENCODING" not in caplog.text
    assert "prometheus_alert_rules/bad-encoding: encoding" in caplog.text


def test_invalid_artifact_schema_does_not_block_telemetry_or_leak_status(caplog):
    secret = "UNIQUE-SECRET-ENCODING"
    with ExitStack() as stack:
        for manager in _patch_runtime():
            stack.enter_context(manager)
        stack.enter_context(
            patch(
                "charm.PrometheusRemoteWriteConsumer.endpoints",
                new_callable=PropertyMock,
                return_value=[{"url": "http://mimir:9009/api/v1/push"}],
            )
        )
        stack.enter_context(patch("charm.alloy.write_config_text"))
        harness = testing.Harness(AlloyCharm)
        harness.set_leader(True)
        harness.begin()
        machine = harness.add_relation("machine-observability", "polkadot")
        prometheus = harness.add_relation("send-remote-write", "mimir")
        good = _rule_artifact("prometheus_alert_rules", "rules", "LAST-GOOD")
        sibling = _rule_artifact("prometheus_alert_rules", "sibling", "SIBLING-V1")
        harness.update_relation_data(
            machine,
            "polkadot",
            {"payload": json.dumps(_v3_payload(good, sibling))},
        )
        invalid = _rule_artifact("prometheus_alert_rules", "rules", "replacement")
        invalid["encoding"] = secret
        invalid["content"] = secret
        invalid["provider-controlled"] = secret
        updated_sibling = _rule_artifact("prometheus_alert_rules", "sibling", "SIBLING-V2")

        harness.update_relation_data(
            machine,
            "polkadot",
            {"payload": json.dumps(_v3_payload(invalid, updated_sibling))},
        )

    groups = json.loads(
        harness.get_relation_data(prometheus, harness.charm.app.name)["alert_rules"]
    )["groups"]
    assert any(group["name"].endswith("rules-LAST-GOOD") for group in groups)
    assert any(group["name"].endswith("sibling-SIBLING-V2") for group in groups)
    assert harness.charm.unit.status.name == "active"
    assert secret not in harness.charm.unit.status.message
    assert secret not in caplog.text


def test_invalid_telemetry_schema_uses_safe_status_without_provider_content(caplog):
    secret = "UNIQUE-SECRET-TELEMETRY"
    with ExitStack() as stack:
        for manager in _patch_runtime():
            stack.enter_context(manager)
        stack.enter_context(patch("charm.alloy.write_config_text"))
        harness = testing.Harness(AlloyCharm)
        harness.begin()
        machine = harness.add_relation("machine-observability", "polkadot")
        payload = _v3_payload()
        payload["metrics_endpoints"] = secret

        harness.update_relation_data(machine, "polkadot", {"payload": json.dumps(payload)})

    assert harness.charm.unit.status.name == "blocked"
    assert harness.charm.unit.status.message.endswith(": schema")
    assert secret not in harness.charm.unit.status.message
    assert secret not in caplog.text


def test_invalid_artifact_identity_does_not_inject_log_content(monkeypatch, caplog):
    import charm as charm_module

    monkeypatch.setattr(
        charm_module.AlloyCharm, "_configure", lambda *args, **kwargs: ops.ActiveStatus()
    )
    harness = testing.Harness(AlloyCharm)
    harness.set_leader(True)
    harness.begin()
    machine = harness.add_relation("machine-observability", "polkadot")
    harness.add_relation("send-remote-write", "mimir")
    artifact = _rule_artifact("prometheus_alert_rules", "valid", "Sensitive")
    artifact["artifact_type"] = "UNIQUE-SECRET-TYPE\nforged-log-line"
    artifact["artifact_id"] = "UNIQUE-SECRET-ID-" + "x" * 1024

    harness.update_relation_data(
        machine, "polkadot", {"payload": json.dumps(_v3_payload(artifact))}
    )

    assert "UNIQUE-SECRET" not in caplog.text
    assert "forged-log-line" not in caplog.text
    assert "unknown/unknown: schema" in caplog.text
