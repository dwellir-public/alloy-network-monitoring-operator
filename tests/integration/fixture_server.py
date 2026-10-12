"""Serve synthetic discovery and metrics on an isolated test collector's loopback."""

import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

DIRECTORY = Path("/tmp/alloy-network-fixture")
STATE = DIRECTORY / "state.json"
HTTP_PORT = 19998


class Handler(BaseHTTPRequestHandler):
    """Accept only the fixture token and expose no credential-bearing responses."""

    successful_revision = 0

    def log_message(self, format: str, *args: object) -> None:
        """Suppress request logging, including authentication headers."""

    def do_GET(self) -> None:  # noqa: N802 - HTTP server protocol
        """Serve HTTP-SD, a gauge and a safe authentication-revision observation."""
        state = json.loads(STATE.read_text())
        status = 200
        content_type = "application/json"
        if self.path.startswith("/api/plugins/prometheus-sd/devices/"):
            if self.headers.get("Authorization") == "Bearer " + state["token"]:
                Handler.successful_revision = state["revision"]
                payload = json.dumps(state["inventory"]).encode()
            else:
                status, payload = 401, b"{}"
        elif self.path == "/metrics":
            payload = (
                b"# TYPE alloy_network_fixture_metric gauge\nalloy_network_fixture_metric 1\n"
            )
            content_type = "text/plain; version=0.0.4"
        elif self.path == "/status":
            payload = json.dumps({"successful_revision": Handler.successful_revision}).encode()
        else:
            status, payload = 404, b"{}"
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)


if __name__ == "__main__":
    ThreadingHTTPServer(("127.0.0.1", HTTP_PORT), Handler).serve_forever()
