"""Outbound endpoint connectivity checks for Grafana Cloud sinks."""

from __future__ import annotations

import base64
import os
import ssl
import tempfile
import urllib.error
import urllib.request

from outbound_endpoints import OutboundEndpoint


def probe_endpoint(endpoint: OutboundEndpoint, *, timeout: float = 5.0) -> tuple[bool, str]:
    """Probe one HTTP endpoint with optional basic auth and CA material."""
    request = urllib.request.Request(endpoint.url, method="HEAD")
    if endpoint.username and endpoint.password:
        credentials = f"{endpoint.username}:{endpoint.password}".encode()
        encoded = base64.b64encode(credentials).decode()
        request.add_header("Authorization", f"Basic {encoded}")

    ca_path = None
    context = None
    if endpoint.tls_ca_pem:
        ca_path = _write_ca_file(endpoint.tls_ca_pem)
        context = ssl.create_default_context(cafile=ca_path)

    try:
        with urllib.request.urlopen(request, timeout=timeout, context=context) as response:
            status_code = response.status
    except urllib.error.HTTPError as exc:
        status_code = exc.code
    except (urllib.error.URLError, ssl.SSLError, ValueError) as exc:
        return False, str(exc)
    finally:
        if ca_path is not None:
            os.unlink(ca_path)

    if status_code in {200, 204, 401, 403, 405}:
        return True, f"http {status_code}"
    return False, f"http {status_code}"


def _write_ca_file(tls_ca_pem: str) -> str:
    """Write CA PEM content to a temporary file for TLS verification."""
    with tempfile.NamedTemporaryFile("w", delete=False) as handle:
        handle.write(tls_ca_pem)
        return handle.name
