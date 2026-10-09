import json
from unittest.mock import patch

from ops import testing

from charm import AlloyCharm

DEFAULT_ARGS = "--server.http.listen-addr=0.0.0.0:6987"
MODEL_NAME = "tenant-routing"
MODEL_UUID = "00000000-0000-4000-8000-000000000111"


def test_send_remote_write_relation_publishes_empty_rules_and_standard_metadata():
    with (
        patch("charm.alloy.ensure_config_dir_permissions"),
        patch("charm.alloy.verify_config"),
        patch("charm.alloy.restart"),
        patch("charm.alloy.reload"),
        patch("charm.AlloyCharm._syslog_receiver_hostname", return_value="alloy-host"),
        patch("charm.AlloyCharm._syslog_receiver_ip", return_value="10.0.0.10"),
        patch("charm.alloy.read_custom_args", return_value=DEFAULT_ARGS),
        patch("charm.alloy.write_custom_args"),
        patch("charm.alloy.write_config_text"),
        patch("charm.AlloyCharm._write_alloy_systemd_unit_defaults"),
    ):
        harness = testing.Harness(AlloyCharm)
        harness.set_leader(True)
        harness.set_model_name(MODEL_NAME)
        harness.set_model_uuid(MODEL_UUID)
        harness.begin()

        relation_id = harness.add_relation("send-remote-write", "mimir-gateway-vm")
        harness.add_relation_unit(relation_id, "mimir-gateway-vm/0")

        relation_data = harness.get_relation_data(relation_id, harness.charm.app.name)

    assert json.loads(relation_data["alert_rules"]) == {"groups": []}
    assert json.loads(relation_data["metadata"]) == {
        "application": harness.charm.app.name,
        "model": MODEL_NAME,
        "model_uuid": MODEL_UUID,
        "unit": f"{harness.charm.app.name}/0",
    }
    assert "tenant-id" not in relation_data
