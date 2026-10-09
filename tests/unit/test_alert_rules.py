import copy
import json
import re
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "lib"))

from charms.dwellir_observability.v0.machine_observability import ArtifactType, encode_artifact

from alert_rules import (
    MAX_RULE_ARTIFACTS,
    CosToolRuleValidator,
    RuleBuildResult,
    publish_rule_groups,
)
from alert_rules import (
    build_rule_state as _build_rule_state,
)

TOPOLOGY = {
    "model": 'prod\\west"1',
    "model_uuid": "00000000-0000-4000-8000-000000000123",
    "application": "polkadot",
    "unit": "polkadot/0",
    "charm_name": "polkadot-node",
}


def _accept_validator(_artifact_type: str, _groups: list[dict[str, object]]) -> bool:
    return True


def build_rule_state(payload: object, validator=_accept_validator) -> RuleBuildResult:
    return _build_rule_state(payload, validator=validator)


def _artifact(artifact_type: ArtifactType, artifact_id: str, groups: list[dict]):
    return encode_artifact(
        artifact_type=artifact_type,
        artifact_id=artifact_id,
        content=json.dumps({"groups": groups}).encode(),
    ).model_dump()


def _raw_artifact(artifact_type: ArtifactType, artifact_id: str, content: str) -> dict:
    return encode_artifact(
        artifact_type=artifact_type,
        artifact_id=artifact_id,
        content=content.encode(),
    ).model_dump()


def _payload(*artifacts: dict, topology: dict | None = None) -> dict:
    return {
        "schema_version": 3,
        "source_topology": TOPOLOGY if topology is None else topology,
        "artifacts": list(artifacts),
    }


def _group(name: str, expr: object = "up{%%juju_topology%%} == 0") -> dict:
    return {
        "name": name,
        "rules": [
            {
                "alert": "WorkloadDown",
                "expr": expr,
                "labels": {"severity": "critical", "juju_model": "spoofed"},
            }
        ],
    }


def _first_group(state: dict[str, list[dict[str, object]]]) -> dict[str, Any]:
    return cast(dict[str, Any], next(iter(state.values()))[0])


def test_build_rule_state_routes_artifacts_by_canonical_ownership_key():
    result = build_rule_state(
        _payload(
            _artifact("loki_alert_rules", "logs", [_group("LogErrors")]),
            _artifact("prometheus_alert_rules", "metrics", [_group("MetricsDown")]),
        )
    )

    prefix = f"{TOPOLOGY['model_uuid']}/{TOPOLOGY['application']}"
    assert set(result.prometheus) == {f"{prefix}/prometheus_alert_rules/metrics"}
    assert set(result.loki) == {f"{prefix}/loki_alert_rules/logs"}
    assert result.errors == ()


def test_build_rule_state_is_deterministic_and_does_not_mutate_input():
    first = _artifact("prometheus_alert_rules", "z-rules", [_group("Zulu"), _group("Alpha")])
    second = _artifact("prometheus_alert_rules", "a-rules", [_group("Beta")])
    payload = _payload(first, second)
    original = copy.deepcopy(payload)

    forward = build_rule_state(payload)
    reverse = build_rule_state(_payload(second, first))

    assert forward == reverse
    assert list(forward.prometheus) == sorted(forward.prometheus)
    assert [group["name"] for groups in forward.prometheus.values() for group in groups] == sorted(
        cast(str, group["name"]) for groups in forward.prometheus.values() for group in groups
    )
    assert payload == original


def test_build_rule_state_uses_original_topology_for_matchers_and_labels():
    result = build_rule_state(
        _payload(
            _artifact(
                "loki_alert_rules",
                "node-alerts",
                [_group("Node alerts", 'sum(rate({%%juju_topology%%} |= "error" [5m]))')],
            )
        )
    )

    rule = _first_group(result.loki)["rules"][0]
    matcher = (
        r'juju_model="prod\\west\"1",'
        r'juju_model_uuid="00000000-0000-4000-8000-000000000123",'
        r'juju_application="polkadot",juju_unit="polkadot/0",juju_charm="polkadot-node"'
    )
    assert rule["expr"] == f'sum(rate({{{matcher}}} |= "error" [5m]))'
    assert rule["labels"] == {
        "severity": "critical",
        "juju_model": TOPOLOGY["model"],
        "juju_model_uuid": TOPOLOGY["model_uuid"],
        "juju_application": TOPOLOGY["application"],
        "juju_unit": TOPOLOGY["unit"],
        "juju_charm": TOPOLOGY["charm_name"],
    }


def test_build_rule_state_replaces_every_placeholder_and_supports_optional_topology_fields():
    topology = {"model_uuid": "principal-uuid", "application": "app", "unit": "app/0"}
    result = build_rule_state(
        _payload(
            _artifact(
                "prometheus_alert_rules",
                "rules",
                [_group("Alerts", "%%juju_topology%% or %%juju_topology%%")],
            ),
            topology=topology,
        )
    )

    rule = _first_group(result.prometheus)["rules"][0]
    matcher = 'juju_model_uuid="principal-uuid",juju_application="app",juju_unit="app/0"'
    assert rule["expr"] == f"{matcher} or {matcher}"
    assert rule["labels"]["juju_application"] == "app"
    assert rule["labels"]["juju_unit"] == "app/0"
    assert "juju_model" not in rule["labels"]


@pytest.mark.parametrize("artifact_type", ["prometheus_alert_rules", "loki_alert_rules"])
def test_build_rule_state_accepts_safe_yaml_rules(artifact_type):
    artifact = _raw_artifact(
        artifact_type,
        "yaml-rules",
        """groups:
  - name: YAML rules
    rules:
      - alert: WorkloadDown
        expr: up{%%juju_topology%%} == 0
        for: 1h30m
        keep_firing_for: 1m
        labels:
          severity: critical
        annotations:
          summary: Workload is down
""",
    )

    result = build_rule_state(_payload(artifact))

    target = result.prometheus if artifact_type == "prometheus_alert_rules" else result.loki
    assert len(next(iter(target.values()))) == 1
    assert result.errors == ()


@pytest.mark.parametrize("duration", ["0", "1h30m"])
def test_build_rule_state_accepts_prometheus_durations(duration):
    artifact = _artifact(
        "prometheus_alert_rules",
        "duration",
        [{"name": "Rules", "rules": [{"alert": "Down", "expr": "up == 0", "for": duration}]}],
    )

    assert build_rule_state(_payload(artifact)).errors == ()


@pytest.mark.parametrize("artifact_type", ["prometheus_alert_rules", "loki_alert_rules"])
def test_build_rule_state_accepts_recording_rules(artifact_type):
    artifact = _raw_artifact(
        artifact_type,
        "recording-rules",
        json.dumps(
            {
                "groups": [
                    {
                        "name": "Recording rules",
                        "rules": [
                            {
                                "record": "workload:up:sum",
                                "expr": "sum(up)",
                                "labels": {"team": "ops"},
                            }
                        ],
                    }
                ]
            }
        ),
    )

    result = build_rule_state(_payload(artifact))

    target = result.prometheus if artifact_type == "prometheus_alert_rules" else result.loki
    assert _first_group(target)["rules"][0]["record"] == "workload:up:sum"
    assert result.errors == ()


@pytest.mark.parametrize("alert_name", ["Disk space low", "Disk-Space-Low", "Disk.Space.Low"])
@pytest.mark.parametrize("artifact_type", ["prometheus_alert_rules", "loki_alert_rules"])
def test_build_rule_state_accepts_human_readable_alert_names(artifact_type, alert_name):
    artifact = _raw_artifact(
        artifact_type,
        "human-alert",
        json.dumps(
            {"groups": [{"name": "Rules", "rules": [{"alert": alert_name, "expr": "up == 0"}]}]}
        ),
    )

    result = build_rule_state(_payload(artifact))

    target = result.prometheus if artifact_type == "prometheus_alert_rules" else result.loki
    assert _first_group(target)["rules"][0]["alert"] == alert_name
    assert result.errors == ()


@pytest.mark.parametrize("artifact_type", ["prometheus_alert_rules", "loki_alert_rules"])
def test_build_rule_state_preserves_valid_group_labels(artifact_type):
    artifact = _raw_artifact(
        artifact_type,
        "group-labels",
        json.dumps(
            {
                "groups": [
                    {
                        "name": "Rules",
                        "labels": {"team": "platform", "environment_name": "production"},
                        "rules": [{"alert": "Disk space low", "expr": "up == 0"}],
                    }
                ]
            }
        ),
    )

    result = build_rule_state(_payload(artifact))

    target = result.prometheus if artifact_type == "prometheus_alert_rules" else result.loki
    group = _first_group(target)
    assert group["labels"] == {"team": "platform", "environment_name": "production"}
    assert group["rules"][0]["labels"]["juju_application"] == TOPOLOGY["application"]
    assert result.errors == ()


@pytest.mark.parametrize("artifact_type", ["prometheus_alert_rules", "loki_alert_rules"])
@pytest.mark.parametrize(
    "topology",
    [
        TOPOLOGY,
        {"model_uuid": "principal-uuid", "application": "app"},
    ],
)
def test_build_rule_state_strips_group_topology_spoofs(artifact_type, topology):
    topology_spoofs = {
        "juju_model": "spoofed-model",
        "juju_model_uuid": "spoofed-uuid",
        "juju_application": "spoofed-app",
        "juju_unit": "spoofed/0",
        "juju_charm": "spoofed-charm",
    }
    artifact = _raw_artifact(
        artifact_type,
        "group-label-spoofs",
        json.dumps(
            {
                "groups": [
                    {
                        "name": "Rules",
                        "labels": {"team": "platform", **topology_spoofs},
                        "rules": [
                            {
                                "alert": "Disk space low",
                                "expr": "up == 0",
                                "labels": {"severity": "critical", **topology_spoofs},
                            }
                        ],
                    }
                ]
            }
        ),
    )

    result = build_rule_state(_payload(artifact, topology=topology))

    target = result.prometheus if artifact_type == "prometheus_alert_rules" else result.loki
    group = _first_group(target)
    expected_topology = {
        label: topology[field]
        for label, field in (
            ("juju_model", "model"),
            ("juju_model_uuid", "model_uuid"),
            ("juju_application", "application"),
            ("juju_unit", "unit"),
            ("juju_charm", "charm_name"),
        )
        if field in topology
    }
    assert group["labels"] == {"team": "platform"}
    assert group["rules"][0]["labels"] == {"severity": "critical", **expected_topology}
    assert result.errors == ()


@pytest.mark.parametrize("artifact_type", ["prometheus_alert_rules", "loki_alert_rules"])
@pytest.mark.parametrize("labels", [{"bad-label": "value"}, {"team": 7}, []])
def test_build_rule_state_rejects_invalid_group_labels(artifact_type, labels):
    artifact = _raw_artifact(
        artifact_type,
        "group-labels",
        json.dumps(
            {
                "groups": [
                    {
                        "name": "Rules",
                        "labels": labels,
                        "rules": [{"alert": "Down", "expr": "up == 0"}],
                    }
                ]
            }
        ),
    )

    result = build_rule_state(_payload(artifact))

    assert result.prometheus == {}
    assert result.loki == {}
    assert result.errors == (f"{artifact_type}/group-labels: schema",)


@pytest.mark.parametrize(
    "content",
    [
        "groups: &groups\n  - name: Unsafe\n    rules: []\ncopy: *groups\n",
        "groups: !unsafe []\n",
    ],
)
def test_build_rule_state_rejects_yaml_anchors_aliases_and_tags(content):
    result = build_rule_state(_payload(_raw_artifact("prometheus_alert_rules", "unsafe", content)))

    assert result.prometheus == {}
    assert result.errors == ("prometheus_alert_rules/unsafe: schema",)


@pytest.mark.parametrize(
    "rule",
    [
        {},
        {"alert": "Down", "record": "down", "expr": "up == 0"},
        {"alert": "", "expr": "up == 0"},
        {"record": "", "expr": "up"},
        {"alert": "Down"},
        {"alert": "Down", "expr": ""},
        {"alert": "Down", "expr": 1},
        {"alert": "Down", "expr": "up", "for": 5},
        {"alert": "Down", "expr": "up", "keep_firing_for": 5},
        {"alert": "Down", "expr": "up", "labels": {"severity": 1}},
        {"alert": "Down", "expr": "up", "unknown": {"attacker": "structure"}},
        {"record": "down", "expr": "up", "for": "5m"},
        {"record": "down", "expr": "up", "keep_firing_for": "5m"},
        {"record": "down", "expr": "up", "annotations": {"summary": "down"}},
        {"alert": "Down", "expr": "up", "for": "five minutes"},
        {"alert": "Down", "expr": "up", "keep_firing_for": "5 minutes"},
        {"alert": "Down", "expr": "up", "for": "30m1h"},
        {"alert": "Down", "expr": "up", "for": "1h2h"},
        {"alert": "Down", "expr": "up", "keep_firing_for": "1ms1s"},
    ],
)
@pytest.mark.parametrize("artifact_type", ["prometheus_alert_rules", "loki_alert_rules"])
def test_build_rule_state_rejects_invalid_rule_semantics(artifact_type, rule):
    artifact = _raw_artifact(
        artifact_type,
        "bad-rule",
        json.dumps({"groups": [{"name": "Rules", "rules": [rule]}]}),
    )

    result = build_rule_state(_payload(artifact))

    assert result.prometheus == {}
    assert result.loki == {}
    assert result.errors == (f"{artifact_type}/bad-rule: schema",)


def test_build_rule_state_rejects_non_string_yaml_mapping_keys():
    artifact = _raw_artifact(
        "prometheus_alert_rules",
        "bad-annotations",
        """groups:
  - name: Rules
    rules:
      - alert: Down
        expr: up
        annotations:
          1: summary
""",
    )

    result = build_rule_state(_payload(artifact))

    assert result.prometheus == {}
    assert result.errors == ("prometheus_alert_rules/bad-annotations: schema",)


@pytest.mark.parametrize(
    "topology",
    [
        None,
        {"application": "polkadot"},
        {"model_uuid": "principal-uuid", "application": ""},
    ],
)
def test_build_rule_state_requires_principal_ownership_topology(topology):
    payload = _payload(
        _artifact("prometheus_alert_rules", "rules", [_group("Rules")]), topology=topology or {}
    )
    if topology is None:
        payload.pop("source_topology")

    result = build_rule_state(payload)

    assert result.prometheus == {}
    assert result.errors == ("prometheus_alert_rules/rules: topology",)


@pytest.mark.parametrize(
    "topology",
    [
        {**TOPOLOGY, "model_uuid": "uuid/forged"},
        {**TOPOLOGY, "application": "app/forged"},
        {**TOPOLOGY, "application": " polkadot"},
    ],
)
def test_build_rule_state_rejects_ambiguous_ownership_topology(topology):
    result = build_rule_state(
        _payload(
            _artifact("prometheus_alert_rules", "rules", [_group("Rules")]), topology=topology
        )
    )

    assert result.prometheus == {}
    assert result.errors == ("prometheus_alert_rules/rules: topology",)


def test_group_names_have_filesystem_safe_unique_readable_prefixes():
    result = build_rule_state(
        _payload(
            _artifact("prometheus_alert_rules", "node.rules", [_group("CPU / load high")]),
            topology={**TOPOLOGY, "application": "my@app"},
        )
    )

    name = _first_group(result.prometheus)["name"]
    assert name.endswith("-node.rules-CPU-load-high")
    assert TOPOLOGY["model_uuid"] in name
    assert re.fullmatch(r"[A-Za-z0-9._-]+", name)


def test_group_names_include_ownership_hash_for_lossy_topology_collisions():
    artifact = _artifact("prometheus_alert_rules", "node.rules", [_group("CPU / load")])
    first = build_rule_state(_payload(artifact, topology={**TOPOLOGY, "application": "my@app"}))
    second = build_rule_state(_payload(artifact, topology={**TOPOLOGY, "application": "my#app"}))

    first_name = _first_group(first.prometheus)["name"]
    second_name = _first_group(second.prometheus)["name"]
    assert first_name != second_name
    assert first_name.endswith("node.rules-CPU-load")
    assert second_name.endswith("node.rules-CPU-load")


def test_sanitized_group_names_remain_unique_and_input_order_independent():
    groups = [_group("CPU / load"), _group("CPU   load")]

    forward = build_rule_state(_payload(_artifact("prometheus_alert_rules", "node.rules", groups)))
    reverse = build_rule_state(
        _payload(_artifact("prometheus_alert_rules", "node.rules", list(reversed(groups))))
    )

    names = [group["name"] for group in next(iter(forward.prometheus.values()))]
    assert len(names) == len(set(names)) == 2
    assert forward == reverse


def test_rule_document_allows_unrelated_top_level_metadata():
    artifact = encode_artifact(
        artifact_type="prometheus_alert_rules",
        artifact_id="metadata",
        content=json.dumps({"groups": [_group("Valid")], "source": "publisher"}).encode(),
    ).model_dump()

    result = build_rule_state(_payload(artifact))

    assert len(next(iter(result.prometheus.values()))) == 1
    assert result.errors == ()


@pytest.mark.parametrize(
    "payload",
    [
        {"schema_version": 1},
        {"schema_version": 2, "source_topology": TOPOLOGY},
        _payload(),
    ],
)
def test_omitted_artifacts_build_empty_desired_state(payload):
    assert build_rule_state(payload) == RuleBuildResult(prometheus={}, loki={}, errors=())


@pytest.mark.parametrize(
    ("content", "category"),
    [
        (b"[]", "schema"),
        (json.dumps({"groups": {}}).encode(), "schema"),
        (json.dumps({"groups": [{"name": "", "rules": []}]}).encode(), "schema"),
        (json.dumps({"groups": [{"name": "   ", "rules": []}]}).encode(), "schema"),
        (json.dumps({"groups": [{"name": "named"}]}).encode(), "schema"),
        (json.dumps({"groups": [{"name": "named", "rules": {}}]}).encode(), "schema"),
        (
            json.dumps({"groups": [{"name": "named", "rules": [], "unknown": {}}]}).encode(),
            "schema",
        ),
    ],
)
def test_rule_document_schema_is_validated(content, category):
    artifact = encode_artifact(
        artifact_type="prometheus_alert_rules", artifact_id="bad-schema", content=content
    ).model_dump()

    result = build_rule_state(_payload(artifact))

    assert result.prometheus == {}
    assert result.errors == (f"prometheus_alert_rules/bad-schema: {category}",)


@pytest.mark.parametrize("failure", ["checksum", "encoding", "json", "schema"])
def test_one_bad_artifact_is_isolated_and_error_never_leaks_content(failure):
    secret = "super-secret-token"
    valid = _artifact("loki_alert_rules", "valid", [_group("Good")])
    if failure == "json":
        bad = encode_artifact(
            artifact_type="prometheus_alert_rules",
            artifact_id="bad",
            content=f": [{secret}".encode(),
        ).model_dump()
    elif failure == "schema":
        bad = _artifact("prometheus_alert_rules", "bad", [{"name": secret, "rules": "wrong"}])
    else:
        bad = _artifact("prometheus_alert_rules", "bad", [_group(secret)])
        if failure == "checksum":
            bad["sha256"] = "0" * 64
        else:
            bad["encoding"] = "plain-text"
            bad["content"] = secret

    result = build_rule_state(_payload(bad, valid))

    assert result.prometheus == {}
    assert len(result.loki) == 1
    assert result.errors == (f"prometheus_alert_rules/bad: {failure}",)
    assert secret not in " ".join(result.errors)


def test_invalid_artifact_identity_is_bounded_and_safe_in_errors():
    artifact = _artifact("prometheus_alert_rules", "valid", [_group("Rules")])
    artifact["artifact_type"] = "UNIQUE-SECRET-TYPE\nforged-log-line"
    artifact["artifact_id"] = "UNIQUE-SECRET-ID-" + "x" * 1024

    result = build_rule_state(_payload(artifact))

    assert result.errors == ("unknown/unknown: schema",)
    assert "UNIQUE-SECRET" not in " ".join(result.errors)


@pytest.mark.parametrize(
    ("artifact_type", "expression"),
    [
        ("prometheus_alert_rules", "this is not promql {"),
        ("loki_alert_rules", "this is not logql {"),
    ],
)
def test_backend_validator_rejection_is_isolated_from_valid_sibling(artifact_type, expression):
    invalid = _artifact(artifact_type, "invalid", [_group("Invalid", expression)])
    valid = _artifact("prometheus_alert_rules", "valid", [_group("Valid", "up == 0")])

    def validator(_kind, groups):
        return all(
            "this is not" not in rule["expr"] for group in groups for rule in group["rules"]
        )

    result = build_rule_state(_payload(invalid, valid), validator=validator)

    assert len(result.prometheus) == 1
    assert result.loki == {}
    assert result.errors == (f"{artifact_type}/invalid: validation",)


@pytest.mark.parametrize(
    "validator", [None, lambda _kind, _groups: (_ for _ in ()).throw(RuntimeError("secret"))]
)
def test_backend_validator_absence_or_error_fails_closed_without_details(validator):
    result = _build_rule_state(
        _payload(_artifact("prometheus_alert_rules", "rules", [_group("Rules")])),
        validator=validator,
    )

    assert result.prometheus == {}
    assert result.errors == ("prometheus_alert_rules/rules: validation",)


def test_cos_tool_validator_fails_closed_when_binary_is_missing(tmp_path):
    validator = CosToolRuleValidator(tmp_path / "missing-cos-tool")

    assert not validator("prometheus_alert_rules", [_group("Rules")])


def test_cos_tool_validator_caps_calls_and_resets_between_reconciles(tmp_path):
    binary = tmp_path / "cos-tool"
    binary.write_bytes(b"test")
    binary.chmod(0o755)
    calls = []

    def runner(*args, **kwargs):
        calls.append((args, kwargs))
        return SimpleNamespace(returncode=0)

    validator = CosToolRuleValidator(binary, runner=runner, clock=lambda: 0.0)
    validator.begin_reconcile()

    assert sum(validator("prometheus_alert_rules", [_group("Rules")]) for _ in range(40)) == 32
    assert len(calls) == 32
    assert all(call[1]["timeout"] <= 3 for call in calls)

    validator.begin_reconcile()

    assert validator("loki_alert_rules", [_group("Rules")])
    assert len(calls) == 33


def test_cos_tool_validator_hanging_processes_cannot_exceed_cumulative_deadline(tmp_path):
    binary = tmp_path / "cos-tool"
    binary.write_bytes(b"test")
    binary.chmod(0o755)
    now = [100.0]
    timeouts = []

    def runner(*_args, **kwargs):
        timeout = kwargs["timeout"]
        timeouts.append(timeout)
        now[0] += timeout
        raise subprocess.TimeoutExpired("cos-tool", timeout)

    validator = CosToolRuleValidator(binary, runner=runner, clock=lambda: now[0])
    validator.begin_reconcile()

    assert not any(validator("prometheus_alert_rules", [_group("Rules")]) for _ in range(40))
    assert len(timeouts) == 5
    assert all(timeout <= 3 for timeout in timeouts)
    assert sum(timeouts) <= 15


@pytest.mark.parametrize(
    "rule",
    [
        {"record": "bad metric", "expr": "up"},
        {"alert": "ValidAlert", "expr": "up", "labels": {"bad-label": "value"}},
    ],
)
def test_build_rule_state_rejects_invalid_backend_identifiers(rule):
    artifact = _raw_artifact(
        "prometheus_alert_rules",
        "invalid-name",
        json.dumps({"groups": [{"name": "Rules", "rules": [rule]}]}),
    )

    assert build_rule_state(_payload(artifact)).errors == (
        "prometheus_alert_rules/invalid-name: schema",
    )


@pytest.mark.parametrize("alert_name", ["", "   ", 7, "bad\nname", "x" * 300])
def test_build_rule_state_rejects_empty_nonstring_or_unsafe_alert_names(alert_name):
    artifact = _raw_artifact(
        "prometheus_alert_rules",
        "invalid-alert",
        json.dumps(
            {"groups": [{"name": "Rules", "rules": [{"alert": alert_name, "expr": "up"}]}]}
        ),
    )

    assert build_rule_state(_payload(artifact)).errors == (
        "prometheus_alert_rules/invalid-alert: schema",
    )


def test_aggregate_limit_rejects_only_overflowing_artifact(monkeypatch):
    import alert_rules

    monkeypatch.setattr(alert_rules, "MAX_TOTAL_DECODED_ARTIFACT_BYTES", 180)
    first = _artifact("prometheus_alert_rules", "a", [_group("A")])
    overflow = _artifact("prometheus_alert_rules", "b", [_group("B")])
    later = encode_artifact(
        artifact_type="loki_alert_rules",
        artifact_id="c",
        content=b'{"groups":[]}',
    ).model_dump()

    result = build_rule_state(_payload(first, overflow, later))

    assert list(result.prometheus) == [
        f"{TOPOLOGY['model_uuid']}/{TOPOLOGY['application']}/prometheus_alert_rules/a"
    ]
    assert list(result.loki) == [
        f"{TOPOLOGY['model_uuid']}/{TOPOLOGY['application']}/loki_alert_rules/c"
    ]
    assert result.errors == ("prometheus_alert_rules/b: size",)


@pytest.mark.parametrize("failure", ["checksum", "schema"])
def test_rejected_artifacts_consume_the_aggregate_decode_budget(monkeypatch, failure):
    import alert_rules

    monkeypatch.setattr(alert_rules, "MAX_TOTAL_DECODED_ARTIFACT_BYTES", 512)
    valid = _artifact("prometheus_alert_rules", "a-valid", [_group("Valid")])
    rejected = []
    for index in range(5):
        content = (
            json.dumps({"groups": [_group("x" * 150)]}).encode()
            if failure == "checksum"
            else json.dumps({"groups": "x" * 300}).encode()
        )
        artifact = encode_artifact(
            artifact_type="prometheus_alert_rules",
            artifact_id=f"z-invalid-{index}",
            content=content,
        ).model_dump()
        if failure == "checksum":
            artifact["sha256"] = "0" * 64
        rejected.append(artifact)

    result = build_rule_state(_payload(*rejected, valid))

    assert any(key.endswith("/a-valid") for key in result.prometheus)
    assert result.errors[0] == f"prometheus_alert_rules/z-invalid-0: {failure}"
    assert result.errors[-1].endswith(": size")


@pytest.mark.parametrize("artifact_count", [100, 223])
def test_rule_artifact_limit_bounds_decode_parse_and_validation_work(monkeypatch, artifact_count):
    import alert_rules

    decode_calls = 0
    parse_calls = 0
    validation_calls = 0
    original_decode = alert_rules._decode_bounded
    original_parse = alert_rules._load_rule_document

    def decode(*args, **kwargs):
        nonlocal decode_calls
        decode_calls += 1
        return original_decode(*args, **kwargs)

    def parse(*args, **kwargs):
        nonlocal parse_calls
        parse_calls += 1
        return original_parse(*args, **kwargs)

    def validator(_artifact_type, _groups):
        nonlocal validation_calls
        validation_calls += 1
        return True

    monkeypatch.setattr(alert_rules, "_decode_bounded", decode)
    monkeypatch.setattr(alert_rules, "_load_rule_document", parse)
    artifacts = [
        _artifact("prometheus_alert_rules", f"rule-{index:03d}", [_group(f"Rule {index:03d}")])
        for index in range(artifact_count)
    ]

    result = _build_rule_state(_payload(*artifacts), validator=validator)

    assert len(result.prometheus) == MAX_RULE_ARTIFACTS
    assert decode_calls == MAX_RULE_ARTIFACTS
    assert parse_calls == MAX_RULE_ARTIFACTS
    assert validation_calls == MAX_RULE_ARTIFACTS
    assert result.errors == (
        f"artifacts: truncated ({artifact_count - MAX_RULE_ARTIFACTS} additional errors)",
    )


def test_publish_rule_groups_writes_full_compact_desired_state_to_every_relation():
    class App:
        name = "alloy-sub"

    app = App()
    relation_one = SimpleNamespace(app=None, data={app: {"alert_rules": "stale"}})
    relation_two = SimpleNamespace(app=None, data={app: {}})
    charm = SimpleNamespace(
        app=app,
        unit=SimpleNamespace(name="alloy-sub/0"),
        model=SimpleNamespace(
            name="alloy-model",
            uuid="alloy-uuid",
            relations={"send-remote-write": [relation_one, relation_two]},
        ),
    )

    publish_rule_groups(charm, "send-remote-write", [_group("Published")])

    expected_rules = json.dumps(
        {"groups": [_group("Published")]}, sort_keys=True, separators=(",", ":")
    )
    for relation in (relation_one, relation_two):
        assert relation.data[charm.app]["alert_rules"] == expected_rules
        assert json.loads(relation.data[charm.app]["metadata"]) == {
            "application": "alloy-sub",
            "model": "alloy-model",
            "model_uuid": "alloy-uuid",
            "unit": "alloy-sub/0",
        }


@pytest.mark.parametrize("advertised", [None, '["json"]', '["lzma", "json"]', "invalid"])
def test_rule_publication_negotiates_and_handles_capability_withdrawal(advertised):
    from cosl import LZMABase64

    class App:
        name = "alloy"

    app, remote = App(), App()
    remote_data = {} if advertised is None else {"alert_rules_encodings": advertised}
    relation = SimpleNamespace(app=remote, data={app: {}, remote: remote_data})
    charm = SimpleNamespace(
        app=app,
        unit=SimpleNamespace(name="alloy/0"),
        model=SimpleNamespace(
            name="local", uuid="test", relations={"send-remote-write": [relation]}
        ),
    )
    group = _group("Published")
    publish_rule_groups(charm, "send-remote-write", [group])
    raw = relation.data[app]["alert_rules"]
    assert json.loads(LZMABase64.decompress(raw) if raw.startswith("/Td6WFoA") else raw) == {
        "groups": [group]
    }
    assert raw.startswith("/Td6WFoA") == (advertised == '["lzma", "json"]')
    remote_data.clear()
    publish_rule_groups(charm, "send-remote-write", [group])
    assert json.loads(relation.data[app]["alert_rules"]) == {"groups": [group]}


@pytest.mark.parametrize("advertised", ['["lzma", "json"]', '["json"]', "null", "{}", '"lzma"'])
def test_publication_uses_public_codec_and_rejects_oversize_without_truncation(
    monkeypatch, advertised
):
    from cosl import LZMABase64

    class App:
        name = "alloy"

    app, remote = App(), App()
    data = {app: {"alert_rules": "prior"}, remote: {"alert_rules_encodings": advertised}}
    relation = SimpleNamespace(app=remote, data=data)
    charm = SimpleNamespace(
        app=app,
        unit=SimpleNamespace(name="alloy/0"),
        model=SimpleNamespace(
            name="local", uuid="test", relations={"send-remote-write": [relation]}
        ),
    )
    calls = []
    original = LZMABase64.compress

    def compress(raw):
        calls.append(raw)
        return original(raw)

    monkeypatch.setattr(LZMABase64, "compress", compress)
    group = _group("large")
    group["rules"][0]["expr"] = "a" * (70 * 1024)
    if advertised == '["lzma", "json"]':
        publish_rule_groups(charm, "send-remote-write", [group])
        assert calls
        assert json.loads(LZMABase64.decompress(data[app]["alert_rules"])) == {"groups": [group]}
    else:
        with pytest.raises(ValueError, match="capacity"):
            publish_rule_groups(charm, "send-remote-write", [group])
        assert data[app]["alert_rules"] == "prior"
        assert not calls
    before = dict(data[app])
    group["rules"][0]["expr"] = "a" * (8 * 1024 * 1024)
    with pytest.raises(ValueError, match="decoded size"):
        publish_rule_groups(charm, "send-remote-write", [group])
    assert data[app] == before
