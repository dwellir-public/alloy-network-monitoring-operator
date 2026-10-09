import stat
from pathlib import Path

import pytest

import secret_files


@pytest.fixture
def directory(tmp_path, monkeypatch):
    path = tmp_path / "network-secrets"
    monkeypatch.setattr(secret_files, "DEFAULT_SECRET_DIR", path)
    return path


def test_generation_rotation_and_permissions(directory):
    original = secret_files.stage_secret_files({"token": "first", "snmp": "profiles"}, {})
    assert original.created
    for filename in original.paths.values():
        assert stat.S_IMODE(Path(filename).stat().st_mode) == 0o640
    assert stat.S_IMODE(directory.stat().st_mode) == 0o750
    unchanged = secret_files.stage_secret_files(
        {"token": "first", "snmp": "profiles"}, original.paths
    )
    assert unchanged.paths == original.paths
    assert not unchanged.created
    rotated = secret_files.stage_secret_files(
        {"token": "second", "snmp": "profiles"}, original.paths
    )
    assert rotated.paths != original.paths
    assert Path(original.paths["token"]).read_text() == "first"
    assert Path(rotated.paths["token"]).read_text() == "second"
    secret_files.remove_secret_files(rotated.paths)
    assert Path(original.paths["token"]).exists()
    assert not Path(rotated.paths["token"]).exists()


def test_cleanup_ignores_unmanaged_paths(directory, tmp_path):
    unrelated = tmp_path / "netbox-token"
    unrelated.write_text("unrelated")
    secret_files.remove_secret_files({"token": str(unrelated)})
    assert unrelated.read_text() == "unrelated"
