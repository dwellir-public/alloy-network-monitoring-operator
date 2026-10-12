"""Publish real Juju source identity for synthetic, colocated metrics and file logs."""

import subprocess
from pathlib import Path

import ops
from charms.dwellir_observability.v0.machine_observability import (
    LogFileSource,
    MachineObservabilityPayload,
    MachineObservabilityProvider,
    MetricsEndpoint,
    SourceTopology,
)


class SourceFixtureCharm(ops.CharmBase):
    """Own an isolated HTTP metric and file log under the unchanged v3 contract."""

    def __init__(self, *args):
        super().__init__(*args)
        self.directory = Path("/tmp") / self.app.name
        self.service = self.app.name + "-fixture"
        self.provider = MachineObservabilityProvider(self, payload_factory=self._payload)
        self.framework.observe(self.on.install, self._start)
        self.framework.observe(self.on.start, self._start)
        self.framework.observe(self.on.stop, self._stop)

    def _start(self, event) -> None:
        """Run the source under its own service, on the test collector's machine."""
        self.directory.mkdir(exist_ok=True)
        self.directory.chmod(0o755)
        (self.directory / "provider.log").write_text(self.app.name + " provider fixture log\n")
        (self.directory / "provider.log").chmod(0o644)
        script = self.directory / "server.py"
        script.write_text((Path(__file__).parent / "service.py").read_text())
        check = subprocess.run(["systemctl", "is-active", "--quiet", self.service], check=False)
        if check.returncode:
            subprocess.run(
                ["systemd-run", "--unit=" + self.service, "/usr/bin/python3", str(script)],
                check=True,
            )
        self.unit.status = ops.ActiveStatus("Synthetic sources ready")
        self.unit.set_workload_version("fixture")

    def _stop(self, event) -> None:
        """Stop only this fixture's systemd service."""
        subprocess.run(["systemctl", "stop", self.service], check=False)

    def _payload(self) -> MachineObservabilityPayload:
        """Publish accurate model/application/unit identity, with no alert artifacts."""
        return MachineObservabilityPayload(
            schema_version=3,
            charm_name=self.meta.name,
            source_topology=SourceTopology(
                model=self.model.name,
                model_uuid=self.model.uuid,
                application=self.app.name,
                unit=self.unit.name,
                charm_name=self.meta.name,
            ),
            metrics_endpoints=[
                MetricsEndpoint(targets=["localhost:19999"], interval="5s", timeout="2s")
            ],
            log_files=[
                LogFileSource(
                    include=[str(self.directory / "provider.log")],
                    attributes={"log_source": "provider_fixture"},
                )
            ],
            artifacts=[],
        )


if __name__ == "__main__":
    ops.main(SourceFixtureCharm)
