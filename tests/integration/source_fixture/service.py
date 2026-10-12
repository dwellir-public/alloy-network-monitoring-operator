"""Serve a synthetic source metric, accessible only on the test machine."""

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


class Handler(BaseHTTPRequestHandler):
    """Expose one metric for provider-topology acceptance."""

    def log_message(self, format: str, *args: object) -> None:
        """Suppress routine fixture request logging."""

    def do_GET(self) -> None:  # noqa: N802 - HTTP server protocol
        """Return an isolated gauge in Prometheus text format."""
        payload = b"# TYPE alloy_provider_fixture_metric gauge\nalloy_provider_fixture_metric 1\n"
        self.send_response(200)
        self.send_header("Content-Type", "text/plain; version=0.0.4")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)


if __name__ == "__main__":
    ThreadingHTTPServer(("127.0.0.1", 19999), Handler).serve_forever()
