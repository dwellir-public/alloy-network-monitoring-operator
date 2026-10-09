import sys
from pathlib import Path
from unittest.mock import PropertyMock, patch

from ops import testing

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "lib"))

from charm import AlloyCharm


def test_grafana_cloud_relation_merges_with_existing_remote_write_and_loki_relations():
    default_args = "--server.http.listen-addr=0.0.0.0:6987"
    with (
        patch("charm.alloy.ensure_config_dir_permissions"),
        patch("charm.alloy.verify_config"),
        patch("charm.alloy.restart"),
        patch("charm.alloy.reload"),
        patch("charm.AlloyCharm._syslog_receiver_hostname", return_value="alloy-host"),
        patch("charm.AlloyCharm._syslog_receiver_ip", return_value="10.0.0.10"),
        patch("charm.alloy.read_custom_args", return_value=default_args),
        patch("charm.alloy.write_custom_args"),
        patch("charm.alloy.write_config_text"),
        patch("charm.AlloyCharm._write_alloy_systemd_unit_defaults"),
        patch(
            "charm.LokiPushApiConsumer.loki_endpoints",
            new_callable=PropertyMock,
            return_value=[{"url": "http://loki:3100/loki/api/v1/push"}],
        ),
        patch(
            "charm.PrometheusRemoteWriteConsumer.endpoints",
            new_callable=PropertyMock,
            return_value=[{"url": "http://mimir:9009/api/v1/push"}],
        ),
    ):
        harness = testing.Harness(AlloyCharm)
        harness.begin()

        cloud_relation_id = harness.add_relation(
            "grafana-cloud-config", "grafana-cloud-integrator"
        )
        harness.add_relation_unit(cloud_relation_id, "grafana-cloud-integrator/0")
        harness.update_relation_data(
            cloud_relation_id,
            "grafana-cloud-integrator",
            {
                "prometheus_url": "https://prometheus-prod-39-prod-eu-north-0.grafana.net/api/prom/push",
                "prometheus_username": "1076854",
                "prometheus_password": "prom-token",
                "loki_url": "https://logs-prod-025.grafana.net/loki/api/v1/push",
                "loki_username": "639149",
                "loki_password": "loki-token",
                "tls-ca": "-----BEGIN CERTIFICATE-----\nabc\n-----END CERTIFICATE-----\n",
            },
        )

        remote_write = harness.charm._remote_write_endpoint_urls()
        loki = harness.charm._loki_endpoint_urls()

        assert [endpoint.url for endpoint in remote_write] == [
            "http://mimir:9009/api/v1/push",
            "https://prometheus-prod-39-prod-eu-north-0.grafana.net/api/prom/push",
        ]
        assert remote_write[1].username == "1076854"
        assert remote_write[1].password == "prom-token"
        assert remote_write[1].tls_ca_pem.startswith("-----BEGIN CERTIFICATE-----")

        assert [endpoint.url for endpoint in loki] == [
            "http://loki:3100/loki/api/v1/push",
            "https://logs-prod-025.grafana.net/loki/api/v1/push",
        ]
        assert loki[1].username == "639149"
        assert loki[1].password == "loki-token"
        assert loki[1].tls_ca_pem.startswith("-----BEGIN CERTIFICATE-----")
