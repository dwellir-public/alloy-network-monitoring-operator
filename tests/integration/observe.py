"""Print safe runtime observations from an isolated acceptance-test collector."""

import hashlib
import json
import re
import subprocess
from pathlib import Path
from typing import Any
from urllib.error import HTTPError
from urllib.request import ProxyHandler, build_opener


def property_value(name: str) -> str:
    """Read one systemd property for the workload under test."""
    return subprocess.check_output(
        ["systemctl", "show", "alloy", "--value", "-p", name], text=True
    ).strip()


def decode(value: dict[str, Any]) -> Any:
    """Decode Alloy's typed component export without reading secret components."""
    kind, data = value["type"], value["value"]
    if kind == "array":
        return [decode(item) for item in data]
    if kind == "object":
        return {item["key"]: decode(item["value"]) for item in data}
    return data


def observe() -> dict:
    """Observe metadata and public profile names, excluding credential content."""
    config = Path("/etc/alloy/config.alloy").read_text()
    files = re.findall(r'/etc/alloy/network-secrets/generation-[^"]+', config)
    client = build_opener(ProxyHandler({}))
    with client.open("http://127.0.0.1:19998/status", timeout=5) as response:
        revision = json.load(response)["successful_revision"]
    pid = property_value("MainPID")
    arguments = Path(f"/proc/{pid}/cmdline").read_bytes().decode().split("\x00")
    storage = None
    for index, argument in enumerate(arguments):
        if argument.startswith("--storage.path="):
            storage = argument.split("=", 1)[1]
        elif argument == "--storage.path" and index + 1 < len(arguments):
            storage = arguments[index + 1]
    wal = Path(storage) / "prometheus.remote_write.metrics" / "wal" if storage else None
    level = re.search(r'level\s*=\s*"([^"]+)"', config)
    targets: list[dict[str, str]] = []
    if 'prometheus.scrape "network_snmp"' in config:
        url = "http://127.0.0.1:6987/api/v0/web/components/discovery.relabel.network_scrape"
        try:
            with client.open(url, timeout=5) as response:
                component = json.load(response)
            exported = next(item for item in component["exports"] if item["name"] == "output")
            targets = decode(exported["value"])
        except HTTPError:
            pass
    return {
        "pid": pid,
        "start_time": property_value("ExecMainStartTimestamp"),
        "config_sha256": hashlib.sha256(config.encode()).hexdigest(),
        "secret_files": files,
        "network_present": 'prometheus.scrape "network_snmp"' in config,
        "storage_path": storage,
        "wal_path": str(wal) if wal else None,
        "wal_segments": sorted(p.name for p in wal.iterdir()) if wal and wal.is_dir() else [],
        "log_level": level[1] if level else None,
        "auth_revision": revision,
        "snmp_auth_profiles": sorted({target["__param_auth"] for target in targets}),
    }


if __name__ == "__main__":
    print(json.dumps(observe()))
