import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from config_builder import ConfigBuilder, FileLogSource, LogSourceGroup

TOPOLOGY = {
    "juju_model": "alloy-model",
    "juju_model_uuid": "00000000-0000-4000-8000-000000000001",
    "juju_application": "alloy-vm",
    "juju_unit": "alloy-vm/0",
    "juju_charm": "alloy-vm",
}


def test_machine_log_source_groups_render_independent_processors():
    rendered = ConfigBuilder(
        loki_endpoints=["http://loki:3100/loki/api/v1/push"],
        remote_write_endpoints=[],
        metrics_scrape_jobs=[],
        systemd_units=[],
        journal_kernel=False,
        journal_match_expressions=[],
        live_debugging=False,
        enable_syslog_receivers=False,
        syslog_drop_access_logs=False,
        syslog_drop_expressions=[],
        syslog_rate_limit=0,
        syslog_rate_burst=0,
        receiver_hostname="",
        receiver_ip="",
        topology_labels=TOPOLOGY,
        log_source_groups=[
            LogSourceGroup(
                component_name="op-node",
                topology_labels={
                    "juju_model": "base-mainnet",
                    "juju_model_uuid": "00000000-0000-4000-8000-000000000002",
                    "juju_application": "op-node",
                    "juju_unit": "op-node/0",
                    "juju_charm": "op-node",
                },
                systemd_units=["opnode.service"],
                file_log_sources=[
                    FileLogSource(
                        include=["/var/log/op-node/*.log"],
                        exclude=["/var/log/op-node/debug.log"],
                        attributes={"service_name": "op-node"},
                    )
                ],
            ),
            LogSourceGroup(
                component_name="op-reth",
                topology_labels={
                    "juju_model": "base-mainnet",
                    "juju_model_uuid": "00000000-0000-4000-8000-000000000002",
                    "juju_application": "op-reth",
                    "juju_unit": "op-reth/0",
                    "juju_charm": "op-reth",
                },
                systemd_units=["op-reth.service"],
            ),
        ],
    ).build()

    assert 'loki.process "op_node"' in rendered
    assert 'loki.process "op_reth"' in rendered
    assert 'juju_application = "op-node"' in rendered
    assert 'juju_application = "op-reth"' in rendered
    assert 'matches = "_SYSTEMD_UNIT=opnode.service"' in rendered
    assert 'matches = "_SYSTEMD_UNIT=op-reth.service"' in rendered
    assert "/var/log/op-node/*.log" in rendered
    assert "/var/log/op-node/debug.log" in rendered
