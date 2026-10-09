"""Manage protected credential generations without changing live files in place."""

from __future__ import annotations

import grp
import os
import shutil
import tempfile
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

DEFAULT_SECRET_DIR = Path("/etc/alloy/network-secrets")
_FILENAMES = {"token": "netbox-token", "snmp": "snmp.yaml", "ca": "netbox-ca.pem"}


@dataclass(frozen=True)
class SecretFiles:
    """Public file references for a newly staged or existing credential generation."""

    paths: dict[str, str]
    created: bool


def _set_permissions(path: Path, mode: int) -> None:
    """Give root ownership and Alloy group access when running as root."""
    if os.geteuid() == 0:
        try:
            gid = grp.getgrnam("alloy").gr_gid
        except KeyError:
            raise OSError("Alloy workload group is not installed") from None
        os.chown(path, 0, gid)
    path.chmod(mode)


def _generation(paths: Mapping[str, str], directory: Path) -> Path | None:
    """Recognize only generations and filenames managed by this helper."""
    if not paths or set(paths) - _FILENAMES.keys():
        return None
    parents = {Path(path).parent for path in paths.values()}
    if len(parents) != 1:
        return None
    parent = parents.pop()
    if parent.parent != directory or not parent.name.startswith("generation-"):
        return None
    if directory.is_symlink() or parent.is_symlink():
        return None
    if any(Path(path).name != _FILENAMES[key] for key, path in paths.items()):
        return None
    if any(Path(path).is_symlink() for path in paths.values()):
        return None
    return parent


def stage_secret_files(contents: Mapping[str, str], previous: Mapping[str, str]) -> SecretFiles:
    """Reuse identical files or stage an immutable protected generation for reload."""
    directory = DEFAULT_SECRET_DIR
    if not contents or set(contents) - _FILENAMES.keys() or directory.is_symlink():
        raise OSError("Invalid managed credential directory or fields")
    if _generation(previous, directory) and set(contents) == set(previous):
        try:
            unchanged = all(
                Path(previous[key]).read_text() == value for key, value in contents.items()
            )
        except OSError:
            unchanged = False
        if unchanged:
            _set_permissions(directory, 0o750)
            _set_permissions(Path(next(iter(previous.values()))).parent, 0o750)
            for path in previous.values():
                _set_permissions(Path(path), 0o640)
            return SecretFiles(dict(previous), created=False)
    directory.mkdir(parents=True, exist_ok=True)
    _set_permissions(directory, 0o750)
    generation = Path(tempfile.mkdtemp(prefix="generation-", dir=directory))
    paths: dict[str, str] = {}
    try:
        _set_permissions(generation, 0o750)
        for key, value in contents.items():
            path = generation / _FILENAMES[key]
            with path.open("x", encoding="utf-8") as stream:
                path.chmod(0o600)
                stream.write(value)
                stream.flush()
                os.fsync(stream.fileno())
            _set_permissions(path, 0o640)
            paths[key] = str(path)
    except Exception:
        shutil.rmtree(generation)
        raise
    return SecretFiles(paths, created=True)


def remove_secret_files(paths: Mapping[str, str]) -> None:
    """Delete an obsolete managed generation, leaving other files untouched."""
    generation = _generation(paths, DEFAULT_SECRET_DIR)
    if generation and generation.exists():
        shutil.rmtree(generation)
