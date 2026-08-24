"""Minimal production HTTP server for the read-only operational dashboard."""

from __future__ import annotations

import html
import json
import os
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

from dashboard.operational import REQUIRED_FIELDS, collect_status


def render_dashboard(status: dict[str, Any]) -> bytes:
    """Render an escaped HTML operational dashboard from a status snapshot."""
    rows = "".join(
        f"<tr><th>{html.escape(field.replace('_', ' ').title())}</th>"
        f"<td>{html.escape(str(status[field]))}</td></tr>"
        for field in REQUIRED_FIELDS
    )
    style = (
        "body{font-family:system-ui;margin:3rem;max-width:48rem}"
        "th{text-align:left;padding-right:2rem}"
        "td{font-family:monospace}.warning{color:#9a6700}"
    )
    source = html.escape(str(status["status_file"]))
    generated_at = html.escape(str(status["generated_at"]))
    body = (
        '<!doctype html><html lang="en"><head><meta charset="utf-8">'
        f"<title>Quant India Operations</title><style>{style}</style></head>"
        "<body><h1>Quant India — RC-1 Operations</h1>"
        '<p class="warning">Read-only status; unknown values require operator '
        "investigation.</p>"
        f"<table>{rows}</table><p>Snapshot: {source}; generated: {generated_at}</p>"
        "</body></html>"
    )
    return body.encode("utf-8")


class DashboardHandler(BaseHTTPRequestHandler):
    """Serve dashboard, JSON status, and health endpoints without side effects."""

    def do_GET(self) -> None:  # noqa: N802
        """Handle a safe read-only GET request."""
        status = collect_status()
        if self.path == "/healthz":
            payload = json.dumps(
                {"status": "ok", "component": "operational-dashboard"}
            ).encode()
            self._send(HTTPStatus.OK, "application/json", payload)
        elif self.path == "/api/status":
            self._send(HTTPStatus.OK, "application/json", json.dumps(status).encode())
        elif self.path == "/":
            self._send(
                HTTPStatus.OK, "text/html; charset=utf-8", render_dashboard(status)
            )
        else:
            self._send(
                HTTPStatus.NOT_FOUND, "text/plain; charset=utf-8", b"Not found\n"
            )

    def log_message(self, format: str, *args: Any) -> None:
        """Avoid unstructured request logs; systemd captures process output."""

    def _send(self, code: HTTPStatus, content_type: str, body: bytes) -> None:
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)


def run_server(port: int | None = None) -> None:
    """Run the dashboard on all interfaces for a container or VPS reverse proxy."""
    actual_port = port if port is not None else int(os.getenv("PORT", "8080"))
    server = ThreadingHTTPServer(("0.0.0.0", actual_port), DashboardHandler)
    server.serve_forever()


if __name__ == "__main__":
    run_server()
