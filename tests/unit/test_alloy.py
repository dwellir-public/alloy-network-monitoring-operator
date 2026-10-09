# Copyright 2025 Erik Lönroth
# See LICENSE file for licensing details.

import subprocess

import alloy


def _completed(*, stdout: str = "", stderr: str = "") -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(
        args=["alloy", "--version"],
        returncode=0,
        stdout=stdout,
        stderr=stderr,
    )


def test_get_version_parses_v_prefix(monkeypatch):
    monkeypatch.setattr(
        alloy,
        "_run",
        lambda *_args, **_kwargs: _completed(
            stdout="alloy, version v1.12.2 (branch: HEAD, revision: 477e314)\n"
        ),
    )

    assert alloy.get_version() == "1.12.2"


def test_get_version_parses_plain_version(monkeypatch):
    monkeypatch.setattr(
        alloy,
        "_run",
        lambda *_args, **_kwargs: _completed(stdout="alloy, version 1.12.2\n"),
    )

    assert alloy.get_version() == "1.12.2"


def test_get_version_returns_none_when_missing(monkeypatch):
    monkeypatch.setattr(
        alloy,
        "_run",
        lambda *_args, **_kwargs: _completed(stdout="alloy build info\n"),
    )

    assert alloy.get_version() is None


def test_read_custom_args(tmp_path):
    defaults = tmp_path / "alloy-defaults"
    defaults.write_text(
        'CONFIG_FILE="/etc/alloy/config.alloy"\n'
        'CUSTOM_ARGS="--server.http.listen-addr=0.0.0.0:6987"\n',
        encoding="utf-8",
    )

    assert alloy.read_custom_args(defaults_path=defaults) == (
        "--server.http.listen-addr=0.0.0.0:6987"
    )


def test_write_custom_args_updates_existing(tmp_path):
    defaults = tmp_path / "alloy-defaults"
    defaults.write_text(
        'CONFIG_FILE="/etc/alloy/config.alloy"\n'
        'CUSTOM_ARGS="--server.http.listen-addr=0.0.0.0:6987"\n',
        encoding="utf-8",
    )

    alloy.write_custom_args(
        "--server.http.listen-addr=0.0.0.0:12345",
        defaults_path=defaults,
    )

    content = defaults.read_text(encoding="utf-8")
    assert 'CUSTOM_ARGS="--server.http.listen-addr=0.0.0.0:12345"' in content
    assert 'CONFIG_FILE="/etc/alloy/config.alloy"' in content


def test_write_custom_args_removes_line_when_none(tmp_path):
    defaults = tmp_path / "alloy-defaults"
    defaults.write_text(
        'CONFIG_FILE="/etc/alloy/config.alloy"\n'
        'CUSTOM_ARGS="--server.http.listen-addr=0.0.0.0:6987"\n',
        encoding="utf-8",
    )

    alloy.write_custom_args(None, defaults_path=defaults)

    content = defaults.read_text(encoding="utf-8")
    assert "CUSTOM_ARGS=" not in content
    assert 'CONFIG_FILE="/etc/alloy/config.alloy"' in content
