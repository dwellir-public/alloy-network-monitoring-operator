"""Normalized outbound sink endpoint models for Alloy config rendering."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class OutboundEndpoint:
    """One outbound endpoint with optional auth and TLS CA material."""

    url: str
    username: str = ""
    password: str = ""
    tls_ca_pem: str = ""


def dedupe_endpoints(endpoints: list[OutboundEndpoint]) -> list[OutboundEndpoint]:
    """Return endpoints in input order with duplicates removed."""
    seen: set[tuple[str, str, str, str]] = set()
    unique: list[OutboundEndpoint] = []
    for endpoint in endpoints:
        key = (endpoint.url, endpoint.username, endpoint.password, endpoint.tls_ca_pem)
        if key in seen:
            continue
        seen.add(key)
        unique.append(endpoint)
    return unique
