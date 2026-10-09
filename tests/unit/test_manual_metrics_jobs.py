# Copyright 2026 Erik Lönroth
# See LICENSE file for licensing details.

import pytest

from manual_metrics_jobs import ManualMetricsJobsError, parse_manual_metrics_jobs

TOPOLOGY = {
    "juju_model": "test-model",
    "juju_model_uuid": "00000000-0000-4000-8000-000000000001",
    "juju_application": "alloy",
    "juju_unit": "alloy/0",
    "juju_charm": "alloy",
}


def test_parse_manual_metrics_jobs_translates_valid_yaml():
    jobs = parse_manual_metrics_jobs(
        """
        - name: external-node-exporter
          targets:
            - "10.20.30.40:9100"
          metrics_path: /metrics
          scheme: https
          scrape_interval: 30s
          scrape_timeout: 10s
          insecure_skip_verify: true
          labels:
            env: prod
            role: node
        """,
        topology_labels=TOPOLOGY,
    )

    assert len(jobs) == 1
    job = jobs[0]
    assert job.job_name == "external-node-exporter"
    assert job.scheme == "https"
    assert job.scrape_interval == "30s"
    assert job.scrape_timeout == "10s"
    assert job.tls_config == {"insecure_skip_verify": True}
    assert job.targets[0].address == "10.20.30.40:9100"
    assert job.targets[0].labels == {
        "env": "prod",
        "role": "node",
        **TOPOLOGY,
    }


def test_parse_manual_metrics_jobs_rejects_reserved_topology_labels():
    message = (
        "manual metrics job 'external-node-exporter' labels must not override reserved juju labels"
    )
    with pytest.raises(
        ManualMetricsJobsError,
        match=message,
    ):
        parse_manual_metrics_jobs(
            """
            - name: external-node-exporter
              targets:
                - "10.20.30.40:9100"
              labels:
                juju_model: fake
            """,
            topology_labels=TOPOLOGY,
        )


def test_parse_manual_metrics_jobs_rejects_duplicate_names():
    with pytest.raises(
        ManualMetricsJobsError,
        match="manual metrics job names must be unique; duplicate name 'external-node-exporter'",
    ):
        parse_manual_metrics_jobs(
            """
            - name: external-node-exporter
              targets:
                - "10.20.30.40:9100"
            - name: external-node-exporter
              targets:
                - "10.20.30.41:9100"
            """,
            topology_labels=TOPOLOGY,
        )
