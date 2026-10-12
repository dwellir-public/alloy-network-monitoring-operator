#!/usr/bin/env python3
"""Fetch the exact Alloy executable used for native pipeline verification."""

from __future__ import annotations

import argparse
import hashlib
import shutil
import tempfile
import urllib.request
import zipfile
from pathlib import Path

VERSION = "1.20.1"
FILENAME = "alloy-linux-amd64"
SHA256 = "4d73d544c8c2baa779bf7efbe8c3906b655416684ff503adfbb5e18c94dbb58d"
URL = f"https://github.com/grafana/alloy/releases/download/v{VERSION}/{FILENAME}.zip"


def checksum(path: Path) -> str:
    """Hash a binary without loading it into memory."""
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def fetch(output: Path) -> Path:
    """Reuse a verified executable or atomically install a checked release."""
    if output.is_file() and checksum(output) == SHA256:
        output.chmod(0o755)
        return output
    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="alloy-download-", dir=output.parent) as name:
        directory = Path(name)
        archive = directory / "release.zip"
        with urllib.request.urlopen(URL, timeout=60) as response, archive.open("wb") as stream:
            shutil.copyfileobj(response, stream)
        candidate = directory / FILENAME
        with zipfile.ZipFile(archive) as bundle:
            with bundle.open(FILENAME) as source, candidate.open("wb") as stream:
                shutil.copyfileobj(source, stream)
        if checksum(candidate) != SHA256:
            raise ValueError("Downloaded Alloy binary does not match the pinned checksum")
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
