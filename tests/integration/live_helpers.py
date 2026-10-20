"""Read safe observations and run synthetic sources on newly created test units."""

import json
import time
import zipfile
from pathlib import Path
from urllib.error import URLError
from urllib.parse import urlencode
from urllib.request import ProxyHandler, build_opener

import jubilant
import pytest
import yaml


def remote_python(juju: jubilant.Juju, unit: str, code: str) -> str:
    """Send Python through stdin rather than a remotely interpreted command string."""
    return juju.cli("ssh", unit, "--", "sudo", "python3", "-", stdin=code)


def build_source_fixture(artifact: Path, output: Path) -> Path:
    """Reuse the built runtime/libraries for a test-only provider, recording distinct bytes."""
    source = Path(__file__).with_name("source_fixture")
    with zipfile.ZipFile(artifact) as original, zipfile.ZipFile(output, "w") as fixture:
        metadata = yaml.safe_load(original.read("metadata.yaml"))
        metadata.update(
            name="network-source-fixture",
            summary="Synthetic source identity acceptance fixture",
            description="Test-only colocated source; no external devices or credentials.",
            provides={"machine-observability": {"interface": "machine_observability"}},
        )
        for key in ("requires", "peers"):
            metadata.pop(key, None)
        for entry in original.infolist():
            if entry.filename == "metadata.yaml":
                data = yaml.safe_dump(metadata).encode()
            elif entry.filename == "src/charm.py":
                data = (source / "charm.py").read_bytes()
            else:
                data = original.read(entry)
            fixture.writestr(entry, data)
        fixture.writestr("src/service.py", (source / "service.py").read_bytes())
    return output


def wait_for(predicate, *, timeout: int = 150):
    """Poll bounded backend observations without mutating the workload."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            result = predicate()
            if result:
                return result
        except (URLError, ConnectionError, json.JSONDecodeError):
            pass
        time.sleep(2)
    pytest.fail("Live collector did not produce the expected scoped observation")


def query(url: str, expression: str, *, logs: bool = False) -> list:
    """Query a supplied lab backend with proxies disabled and a scoped selector."""
    parameters = {"query": expression}
    if logs:
        parameters.update(
            start=str(int((time.time() - 180) * 1e9)),
            end=str(int(time.time() * 1e9)),
            limit="100",
        )
    with build_opener(ProxyHandler({})).open(url + "?" + urlencode(parameters), timeout=10) as r:
        response = json.load(r)
    assert response["status"] == "success"
    return response["data"]["result"]


def indexed_log_labels(query_url: str, selector: str) -> list[dict[str, str]]:
    """Read indexed labels separately from metadata that Loki merges into query results."""
    url = query_url.rsplit("/", 1)[0] + "/series?" + urlencode({"match[]": selector})
    with build_opener(ProxyHandler({})).open(url, timeout=10) as response:
        body = json.load(response)
    assert body["status"] == "success"
    return body["data"]


def start_source(juju: jubilant.Juju, unit: str) -> None:
    """Start fake NetBox/metrics on a new test machine; never contact real devices."""
    source = Path(__file__).with_name("fixture_server.py").read_text()
    state = {
        "token": "fixture-token-1",
        "revision": 1,
        "inventory": [
            {
                "targets": ["127.0.0.2"],
                "labels": {
                    "__meta_netbox_id": "1",
                    "__meta_netbox_name": "fixture-router",
                    "__meta_netbox_primary_ip4": "127.0.0.2",
                    "__meta_netbox_site_slug": "fixture",
                    "__meta_netbox_role_slug": "edge-router",
                    "__meta_netbox_status": "active",
                    "__meta_netbox_tag_slugs": "snmp",
                    "__meta_netbox_custom_field_snmp_auth": "fixture",
                },
            }
        ],
    }
    remote_python(
        juju,
        unit,
        "from pathlib import Path\nimport json, subprocess\n"
        "p=Path('/tmp/alloy-network-fixture');p.mkdir(exist_ok=True)\n"
        f"(p/'server.py').write_text({source!r})\n"
        f"(p/'state.json').write_text({json.dumps(state)!r})\n"
        "subprocess.run(['systemd-run','--unit=alloy-network-fixture',"
        "'/usr/bin/python3',str(p/'server.py')],check=True)\n",
    )


def rotate_source(juju: jubilant.Juju, unit: str) -> None:
    """Advance the fake NetBox's token expectation without revealing token content."""
    remote_python(
        juju,
        unit,
        "import json\nfrom pathlib import Path\n"
        "p=Path('/tmp/alloy-network-fixture/state.json')\n"
        "s=json.loads(p.read_text());s.update(token='fixture-token-2',revision=2)\n"
        "s['inventory'][0]['labels']['__meta_netbox_custom_field_snmp_auth']='rotated'\n"
        "q=p.with_suffix('.new');q.write_text(json.dumps(s));q.replace(p)\n",
    )


def snapshot(juju: jubilant.Juju, unit: str) -> dict:
    """Observe metadata and public profile names without exposing secret content."""
    code = Path(__file__).with_name("observe.py").read_text()
    return json.loads(remote_python(juju, unit, code))


def emit_logs(
    juju: jubilant.Juju, unit: str, message: str, *, application: str = "fixture-app"
) -> None:
    """Emit synthetic syslog and journal messages only on the isolated test unit."""
    remote_python(
        juju,
        unit,
        "import socket,subprocess\nfrom datetime import datetime,timezone\n"
        "stamp=datetime.now(timezone.utc).isoformat().replace('+00:00','Z')\n"
        f"message={message!r}\n"
        f"application={application!r}\n"
        "payload=f'<14>1 {stamp} fixture-router {application} 123 fixture - {message}'\n"
        "with socket.socket(socket.AF_INET,socket.SOCK_DGRAM) as s:\n"
        " s.sendto(payload.encode(),('127.0.0.1',1514))\n"
        "subprocess.run(['logger','-t','alloy-network-fixture',message],check=True)\n",
    )


def set_inventory_modules(juju: jubilant.Juju, unit: str, modules: str) -> None:
    """Change public module metadata in this test's loopback inventory only."""
    remote_python(
        juju,
        unit,
        "import json\nfrom pathlib import Path\n"
        "p=Path('/tmp/alloy-network-fixture/state.json')\n"
        "s=json.loads(p.read_text())\n"
        f"s['inventory'][0]['labels']['__meta_netbox_custom_field_snmp_modules']={modules!r}\n"
        "q=p.with_suffix('.new');q.write_text(json.dumps(s));q.replace(p)\n",
    )


def network_target_modules(juju: jubilant.Juju, unit: str) -> list[str]:
    """Read public selected modules from the fixture unit's Alloy component API."""
    return json.loads(
        remote_python(
            juju,
            unit,
            "import json\nfrom urllib.request import urlopen\n"
            "with urlopen('http://127.0.0.1:6987/api/v0/web/components/'"
            "'discovery.relabel.network_scrape',timeout=5) as r: data=json.load(r)\n"
            "def decode(item):\n"
            " kind,value=item['type'],item['value']\n"
            " if kind=='array': return [decode(x) for x in value]\n"
            " if kind=='object': return {x['key']:decode(x['value']) for x in value}\n"
            " return value\n"
            "targets=decode(next(x['value'] for x in data['exports'] if x['name']=='output'))\n"
            "print(json.dumps([x.get('__param_module','') for x in targets]))\n",
        )
    )
