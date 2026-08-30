"""Production HTTP server for the Quant India research cockpit.

Serves the research cockpit (strategy selection, experiment execution,
results, and history) as the primary interface. Also exposes the
read-only operational dashboard at /operations.

All research execution is delegated to the existing research engine
via ``dashboard.research_api``.  This server never re-implements
pipeline logic.
"""

from __future__ import annotations

import html
import json
import logging
import os
import sys
import threading
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import unquote

# Allow `python dashboard/server.py` to run from a checkout without the
# package being installed: make the repository root importable.
if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from dashboard.operational import REQUIRED_FIELDS, collect_status

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Operational dashboard (preserved from original)
# ---------------------------------------------------------------------------


def render_operational_dashboard(status: dict[str, Any]) -> bytes:
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
        '<p><a href="/">← Strategy Dashboard</a> · '
        '<a href="/cockpit">Research Cockpit</a></p>'
        "</body></html>"
    )
    return body.encode("utf-8")


# Backward-compatible alias for the original render function name.
render_dashboard = render_operational_dashboard


# ---------------------------------------------------------------------------
# Research cockpit page
# ---------------------------------------------------------------------------

_cockpit_cache: bytes | None = None
_cockpit_lock = threading.Lock()


def _get_cockpit_page() -> bytes:
    """Return the cockpit HTML, generating it once and caching."""
    global _cockpit_cache
    if _cockpit_cache is not None:
        return _cockpit_cache
    with _cockpit_lock:
        if _cockpit_cache is not None:
            return _cockpit_cache
        from dashboard.cockpit_html import render_cockpit_page
        from dashboard.research_api import get_data_status, list_strategies

        try:
            strategies = list_strategies()
            data_status = get_data_status()
        except Exception as exc:  # noqa: BLE001
            logger.exception("cockpit_data_unavailable")
            strategies = {}
            data_status = {"error": str(exc)}
        _cockpit_cache = render_cockpit_page(strategies, data_status)
        return _cockpit_cache


# ---------------------------------------------------------------------------
# HTTP handler
# ---------------------------------------------------------------------------


class DashboardHandler(BaseHTTPRequestHandler):
    """Serve the research cockpit and operational dashboard."""

    def do_GET(self) -> None:  # noqa: N802
        """Handle GET requests for the dashboard pages and API endpoints."""
        path = self.path.split("?")[0]
        query = {}
        if "?" in self.path:
            for pair in self.path.split("?", 1)[1].split("&"):
                if "=" in pair:
                    k, v = pair.split("=", 1)
                    query[k] = unquote(v)

        if path == "/healthz":
            self._send_json(
                HTTPStatus.OK,
                {"status": "ok", "component": "research-cockpit"},
            )
        elif path == "/api/status":
            self._send_json(HTTPStatus.OK, collect_status())
        elif path == "/":
            from dashboard.strategy_dashboard import render_strategy_page

            capital = float(query.get("capital", 100_000))
            try:
                self._send(
                    HTTPStatus.OK,
                    "text/html; charset=utf-8",
                    render_strategy_page(capital),
                )
            except Exception:  # noqa: BLE001 — never let the landing page 500
                logger.exception("strategy_dashboard_render_failed")
                self._send(
                    HTTPStatus.INTERNAL_SERVER_ERROR,
                    "text/plain; charset=utf-8",
                    b"Strategy dashboard failed to render; see server log.\n",
                )
        elif path == "/cockpit":
            self._send(HTTPStatus.OK, "text/html; charset=utf-8", _get_cockpit_page())
        elif path == "/operations":
            status = collect_status()
            self._send(
                HTTPStatus.OK,
                "text/html; charset=utf-8",
                render_operational_dashboard(status),
            )
        elif path == "/api/strategy/signal":
            from dashboard.strategy_dashboard import build_signal_payload

            capital = float(query.get("capital", 100_000))
            try:
                self._send_json(HTTPStatus.OK, build_signal_payload(capital))
            except Exception as exc:  # noqa: BLE001
                logger.exception("strategy_signal_failed")
                self._send_json(
                    HTTPStatus.INTERNAL_SERVER_ERROR,
                    {"error": str(exc)},
                )
        elif path in ("/api/strategies", "/api/research/strategies"):
            from dashboard.research_api import list_strategies

            self._send_json(HTTPStatus.OK, list_strategies())
        elif path in ("/api/data-status", "/api/research/data-status"):
            from dashboard.research_api import get_data_status

            self._send_json(HTTPStatus.OK, get_data_status())
        elif path == "/api/research/experiments":
            from dashboard.research_api import list_experiments

            self._send_json(HTTPStatus.OK, list_experiments())
        elif path.startswith("/api/research/experiment/"):
            run_id = unquote(path[len("/api/research/experiment/") :])
            from dashboard.research_api import get_experiment

            exp = get_experiment(run_id)
            if exp is None:
                self._send_json(HTTPStatus.NOT_FOUND, {"error": "not found"})
            else:
                self._send_json(HTTPStatus.OK, exp)
        else:
            self._send(
                HTTPStatus.NOT_FOUND, "text/plain; charset=utf-8", b"Not found\n"
            )

    def do_POST(self) -> None:  # noqa: N802
        """Handle POST requests for launching research runs."""
        path = self.path.split("?")[0]

        if path == "/api/research/run":
            self._handle_research_run()
        else:
            self._send(
                HTTPStatus.NOT_FOUND, "text/plain; charset=utf-8", b"Not found\n"
            )

    def _handle_research_run(self) -> None:
        """Execute a research experiment from a JSON POST body."""
        try:
            content_length = int(self.headers.get("Content-Length", 0))
            body = self.rfile.read(content_length)
            config = json.loads(body)
        except (ValueError, json.JSONDecodeError) as exc:
            self._send_json(HTTPStatus.BAD_REQUEST, {"error": f"invalid JSON: {exc}"})
            return

        strategy = config.get("strategy")
        if not strategy:
            self._send_json(HTTPStatus.BAD_REQUEST, {"error": "strategy is required"})
            return

        try:
            from dashboard.research_api import run_experiment

            result = run_experiment(
                strategy_name=strategy,
                parameters=config.get("parameters"),
                prices_path=config.get("prices_path"),
                use_synthetic=config.get("use_synthetic", False),
                train_size=config.get("train_size", 252),
                test_size=config.get("test_size", 63),
                step_size=config.get("step_size"),
                expanding=config.get("expanding", False),
                placebo_samples=config.get("placebo_samples", 50),
                seed=config.get("seed", 42),
            )
            self._send_json(HTTPStatus.OK, result.to_dict())
        except Exception as exc:
            logger.exception("research_run_failed")
            self._send_json(
                HTTPStatus.INTERNAL_SERVER_ERROR,
                {"error": str(exc)},
            )

    def log_message(self, format: str, *args: Any) -> None:
        """Log to the standard logger instead of stderr."""
        logger.info("%s - %s", self.client_address[0], format % args)

    def _send_json(self, code: HTTPStatus, data: Any) -> None:
        body = json.dumps(data, default=str, sort_keys=True).encode("utf-8")
        self._send(code, "application/json", body)

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
    server = ThreadingHTTPServer(("0.0.0.0", actual_port), DashboardHandler)  # nosec B104
    print(f"Quant India Research Cockpit: http://0.0.0.0:{actual_port}/")
    server.serve_forever()


if __name__ == "__main__":
    run_server()
