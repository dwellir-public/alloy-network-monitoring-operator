#!/usr/bin/env python3
"""Fetch pinned Prometheus promtool for numerical alert-rule tests."""

from __future__ import annotations

import argparse
import hashlib
import shutil
import tarfile
import tempfile
import urllib.request
from pathlib import Path

VERSION = "3.2.1"
ARCHIVE = f"prometheus-{VERSION}.linux-amd64.tar.gz"
SHA256 = "a622e3007c9109a7f470e1433cbd29bf392596715cf7eea8b81b37fa9d26b7be"
URL = f"https://github.com/prometheus/prometheus/releases/download/v{VERSION}/{ARCHIVE}"


def fetch(output: Path) -> Path:
    """Verify the release archive and atomically install only its promtool member."""
    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="promtool-download-", dir=output.parent) as name:
        directory = Path(name)
        archive = directory / ARCHIVE
        with urllib.request.urlopen(URL, timeout=60) as response, archive.open("wb") as stream:
            shutil.copyfileobj(response, stream)
        digest = hashlib.sha256()
        with archive.open("rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(chunk)
        if digest.hexdigest() != SHA256:
            raise ValueError("Downloaded Prometheus release does not match the pinned checksum")
        candidate = directory / "promtool"
        with tarfile.open(archive) as bundle:
            member = bundle.getmember(f"prometheus-{VERSION}.linux-amd64/promtool")
            if not member.isfile():
                raise ValueError("Promtool archive member must be a regular file")
            source = bundle.extractfile(member)
            if source is None:
                raise ValueError("Promtool archive member is missing")
            with source, candidate.open("wb") as stream:
                shutil.copyfileobj(source, stream)
        candidate.chmod(0o755)
        candidate.replace(output)
    return output


def main() -> None:
    """Read the destination and print the verified executable path."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    print(fetch(parser.parse_args().output.resolve()))


if __name__ == "__main__":
    main()
