"""Local dashboard server for virtual paper trading and research.

The paper page is the primary interface: it reads Upstox quotes and maintains a
separate local virtual account. Research execution remains delegated to the
existing research engine, and operational state remains read-only.
"""

from __future__ import annotations

import hmac
import html
import ipaddress
import json
import logging
import os
import queue
import sys
import threading
import time
from datetime import UTC, datetime
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

_paper_service: Any | None = None
_paper_service_lock = threading.Lock()

_live_feed: Any | None = None
_live_feed_lock = threading.Lock()

WEB_DIR = Path(__file__).resolve().parent / "live" / "web"
APP_DIR = Path(__file__).resolve().parent / "app"
GUIDE_FILE = Path(__file__).resolve().parents[1] / "docs" / "BEGINNER_GUIDE.md"
_STATIC_TYPES = {
    ".html": "text/html; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".js": "text/javascript; charset=utf-8",
    ".svg": "image/svg+xml",
}

# ---------------------------------------------------------------------------
# Access control (AUDIT-039)
# ---------------------------------------------------------------------------
# The dashboard mutates operator state — it can arm *and disarm* the kill
# switch, reset the paper account, and trigger rebalances.  Before this change
# it bound ``0.0.0.0`` with no authentication, authorisation, CSRF or origin
# check on any route, so any host that could reach the port could disarm the
# kill switch with a single unauthenticated ``curl``.
#
# The controls below, in order of preference:
#
# 1. **Bind to loopback by default.**  A deployment that wants to listen on a
#    routable interface must set ``QUANT_DASHBOARD_BIND`` explicitly.
# 2. **Fail closed on a routable bind.**  If the bind address is not loopback
#    and ``QUANT_DASHBOARD_TOKEN`` is empty, the server refuses to start
#    unless ``QUANT_DASHBOARD_ALLOW_UNAUTHENTICATED=1`` is set.  That escape
#    hatch exists for supervised local demos and is logged loudly.
# 3. **Origin check** on every mutating request, which is what actually stops
#    a browser on a hostile page from disarming the switch (CSRF).
# 4. **Shared-secret header** ``X-Quant-Token`` on every mutating route.
#
# Honest limitation: the SPA is served by this same server, so when
# ``QUANT_DASHBOARD_TOKEN_IN_UI=1`` the token is handed to any client that can
# load the page.  The token is therefore **not** an authentication boundary for
# browser users — it stops unauthenticated scripts, scanners and CSRF.  A real
# deployment must put an authenticating reverse proxy in front and bind the
# dashboard to loopback.
TOKEN_ENV = "QUANT_DASHBOARD_TOKEN"
BIND_ENV = "QUANT_DASHBOARD_BIND"
ALLOW_INSECURE_ENV = "QUANT_DASHBOARD_ALLOW_UNAUTHENTICATED"
TOKEN_IN_UI_ENV = "QUANT_DASHBOARD_TOKEN_IN_UI"
DEFAULT_BIND = "127.0.0.1"


class DashboardAccessError(RuntimeError):
    """Raised when the dashboard is started with an unsafe configuration."""


def dashboard_token() -> str:
    """Return the configured shared secret (empty when none is set)."""
    return os.getenv(TOKEN_ENV, "").strip()


def _is_loopback(address: str) -> bool:
    try:
        return ipaddress.ip_address(address.strip()).is_loopback
    except ValueError:
        # Not a literal IP (e.g. a hostname). Treat only the obvious local
        # names as loopback; anything else is assumed routable.
        return address.strip().lower() in {"localhost", "localhost.localdomain"}


def resolve_bind(bind: str | None = None) -> str:
    """Resolve the bind address and refuse an unsafe combination.

    Raises :class:`DashboardAccessError` when listening on a routable
    interface without a shared secret and without an explicit override.
    """
    resolved = (bind or os.getenv(BIND_ENV) or DEFAULT_BIND).strip() or DEFAULT_BIND
    if not dashboard_token() and not _is_loopback(resolved):
        if os.getenv(ALLOW_INSECURE_ENV, "").strip().lower() not in {
            "1",
            "true",
            "yes",
        }:
            raise DashboardAccessError(
                f"refusing to bind the dashboard to {resolved!r} without "
                f"{TOKEN_ENV}: every mutating route (including "
                "POST /api/kill-switch) would be reachable unauthenticated. "
                f"Set {TOKEN_ENV}, bind to 127.0.0.1, or set "
                f"{ALLOW_INSECURE_ENV}=1 to acknowledge the risk."
            )
        logger.warning(
            "dashboard_unauthenticated_bind address=%s — every mutating route "
            "is reachable without a token",
            resolved,
        )
    return resolved


def _token_in_ui() -> bool:
    """Whether the server may hand the shared secret to the SPA shell."""
    flag = os.getenv(TOKEN_IN_UI_ENV, "").strip().lower()
    return flag in {"1", "true", "yes"}


# ---------------------------------------------------------------------------
# Health (AUDIT-028)
# ---------------------------------------------------------------------------
# ``/healthz`` used to need only the standard library, so the Docker and
# compose healthchecks reported the container **healthy** while every
# ``/api/*`` panel was failing. Verified before the fix: with the
# third-party imports blocked, ``/healthz`` answered ``{"status": "ok"}``
# while ``get_paper_service()`` raised ``ModuleNotFoundError: No module
# named 'numpy'``. A healthcheck that cannot observe a broken dependency is
# worse than no healthcheck, because it tells the orchestrator to keep
# routing traffic to a dead container.
#
# The check below imports the modules that every API panel depends on and
# touches one attribute from each, so a missing or broken dependency turns
# into a 503 with a readable reason instead of a silent "ok".
CRITICAL_SUBSYSTEMS: tuple[tuple[str, str, str], ...] = (
    ("paper_trading", "paper_trading.service", "PaperTradingService"),
    ("operations", "dashboard.operations", "build_report"),
    ("data_panel", "datahub.panel", "data_status"),
    ("kill_switch", "datahub.kill_switch", "is_killed"),
)

#: Third-party packages the API depends on. These are probed *by name* as
#: well as through the subsystem imports above, because an already-imported
#: module is served from ``sys.modules`` without a fresh import — a probe
#: that only touched our own modules would keep reporting "ok" long after
#: the environment around them broke.
RUNTIME_DEPENDENCIES: tuple[str, ...] = (
    "numpy",
    "pandas",
    "duckdb",
    "pydantic",
    "pyarrow",
)


def subsystem_health() -> dict[str, Any]:
    """Import every critical subsystem and report which ones are broken.

    Returns ``{"status": "ok"|"degraded", "subsystems": {...}, "failed": [...]}.
    Never raises: the kill-switch entry is read-only and the rest are imports.
    """
    import importlib

    subsystems: dict[str, str] = {}
    failed: list[str] = []
    for label, module_name, attribute in CRITICAL_SUBSYSTEMS:
        try:
            module = importlib.import_module(module_name)
            getattr(module, attribute)
        except Exception as exc:  # noqa: BLE001 - report, never raise
            subsystems[label] = f"{type(exc).__name__}: {exc}"
            failed.append(label)
        else:
            subsystems[label] = "ok"
    for package in RUNTIME_DEPENDENCIES:
        label = f"dependency:{package}"
        try:
            importlib.import_module(package)
        except Exception as exc:  # noqa: BLE001 - report, never raise
            subsystems[label] = f"{type(exc).__name__}: {exc}"
            failed.append(label)
        else:
            subsystems[label] = "ok"
    return {
        "status": "ok" if not failed else "degraded",
        "subsystems": subsystems,
        "failed": failed,
    }


def get_live_feed() -> Any:
    """Create the one live-terminal feed owned by this server.

    The feed is a clearly-labelled SIMULATED intraday market built from the
    verified EOD history plus a demo AI trader that writes only to the local
    virtual ledger.  It has no broker access.
    """
    global _live_feed
    if _live_feed is not None:
        return _live_feed
    with _live_feed_lock:
        if _live_feed is None:
            from dashboard.live.feed import LiveFeed

            root = Path(__file__).resolve().parents[1]
            _live_feed = LiveFeed(root)
            _live_feed.start()
    return _live_feed


def get_paper_service() -> Any:
    """Create the one local virtual-paper service owned by this server.

    Construction performs no network request and reads no credentials into any
    HTTP response.  The service is deliberately separate from broker adapters:
    it can only obtain read-only market quotes.
    """
    global _paper_service
    if _paper_service is not None:
        return _paper_service
    with _paper_service_lock:
        if _paper_service is None:
            from paper_trading import PaperLedger, PaperTradingService

            root = Path(__file__).resolve().parents[1]
            db_path = Path(
                os.getenv("QUANT_PAPER_DB", str(root / "var" / "paper_trading.sqlite"))
            )
            _paper_service = PaperTradingService(
                root=root,
                ledger=PaperLedger(db_path),
                quote_stale_seconds=int(
                    os.getenv("QUANT_PAPER_QUOTE_REFRESH_SECONDS", "30")
                ),
            )
    return _paper_service


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
# Unified API dispatch
# ---------------------------------------------------------------------------

INF = float("inf")
NEG_INF = float("-inf")


def _json_safe(value: Any, _depth: int = 0) -> Any:
    """Recursively convert a payload into something ``json.dumps`` can emit.

    Non-finite floats become ``None`` (JSON has no NaN/Infinity).  NumPy scalars
    become native Python numbers so they serialise as numbers rather than being
    stringified by ``default=str``.  Sets and tuples become lists.
    """
    if _depth > 30:  # a self-referencing payload must not hang the server
        return str(value)
    if value is None or isinstance(value, (str, bool)):
        return value
    if isinstance(value, float):
        return value if value == value and value not in (INF, NEG_INF) else None
    if isinstance(value, int):
        return value
    if isinstance(value, dict):
        return {str(k): _json_safe(v, _depth + 1) for k, v in value.items()}
    if isinstance(value, (list, tuple, set, frozenset)):
        return [_json_safe(v, _depth + 1) for v in value]
    # numpy scalars and anything else numeric-ish
    to_float = getattr(value, "item", None)
    if callable(to_float):
        try:
            return _json_safe(to_float(), _depth + 1)
        except (TypeError, ValueError):
            pass
    try:
        number = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return str(value)
    return number if number == number and number not in (INF, NEG_INF) else None


_API_ROUTES = frozenset(
    {
        "/api/overview",
        "/api/divergence",
        "/api/cost-sensitivity",
        "/api/correlation",
        "/api/sizing",
        "/api/regime",
        "/api/operations",
        "/api/universe",
        "/api/research/check",
    }
)


def _dispatch_api(path: str, query: dict[str, str]) -> dict[str, Any]:
    """Build one unified-dashboard payload. See :mod:`dashboard.api`."""
    from dashboard import api as unified

    def _capital() -> float:
        try:
            return float(query.get("capital", 100_000))
        except (TypeError, ValueError):
            return 100_000.0

    if path == "/api/overview":
        return unified.overview_payload(_capital())
    if path == "/api/divergence":
        return unified.divergence_payload(_capital())
    if path == "/api/cost-sensitivity":
        return unified.cost_sensitivity_payload()
    if path == "/api/correlation":
        return unified.correlation_payload()
    if path == "/api/sizing":
        return unified.sizing_payload(_capital())
    if path == "/api/regime":
        return unified.regime_payload()
    if path == "/api/operations":
        return unified.operations_payload()
    if path == "/api/universe":
        from datahub.universe import status as universe_status

        return universe_status()
    if path == "/api/research/check":
        return unified.research_check_payload()
    return {"error": "unknown endpoint"}


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
            health = subsystem_health()
            health["component"] = "research-cockpit"
            health["checked_at"] = datetime.now(UTC).isoformat()
            self._send_json(
                HTTPStatus.OK
                if health["status"] == "ok"
                else HTTPStatus.SERVICE_UNAVAILABLE,
                health,
            )
        elif path == "/api/status":
            self._send_json(HTTPStatus.OK, collect_status())
        elif path == "/live":
            try:
                body = (WEB_DIR / "live_terminal.html").read_bytes()
                self._send(
                    HTTPStatus.OK,
                    "text/html; charset=utf-8",
                    self._with_bootstrap(body),
                )
            except OSError:
                self._send(
                    HTTPStatus.NOT_FOUND,
                    "text/plain; charset=utf-8",
                    b"live terminal assets missing\n",
                )
        elif path.startswith("/live/static/"):
            name = Path(path[len("/live/static/") :]).name
            static_file = WEB_DIR / name
            suffix = static_file.suffix
            try:
                body = static_file.read_bytes()
            except OSError:
                self._send(
                    HTTPStatus.NOT_FOUND, "text/plain; charset=utf-8", b"not found\n"
                )
            else:
                content_type = _STATIC_TYPES.get(suffix, "application/octet-stream")
                self._send(HTTPStatus.OK, content_type, body)
        elif path == "/api/live/state":
            try:
                self._send_json(HTTPStatus.OK, get_live_feed().snapshot())
            except Exception:  # noqa: BLE001
                logger.exception("live_state_failed")
                self._send_json(
                    HTTPStatus.SERVICE_UNAVAILABLE, {"error": "live feed unavailable"}
                )
        elif path == "/api/live/candles":
            symbol = str(query.get("symbol", "RELIANCE")).upper()
            interval = str(query.get("interval", "1m"))
            try:
                limit = int(query.get("limit", "600"))
            except ValueError:
                limit = 600
            try:
                self._send_json(
                    HTTPStatus.OK, get_live_feed().candles(symbol, interval, limit)
                )
            except KeyError as exc:
                self._send_json(HTTPStatus.NOT_FOUND, {"error": str(exc).strip("'")})
            except ValueError as exc:
                self._send_json(HTTPStatus.BAD_REQUEST, {"error": str(exc)})
            except Exception:  # noqa: BLE001
                logger.exception("live_candles_failed")
                self._send_json(
                    HTTPStatus.SERVICE_UNAVAILABLE, {"error": "live feed unavailable"}
                )
        elif path == "/api/live/stream":
            self._serve_live_stream()
        elif path == "/":
            # The unified shell. One tab, one process, one data layer.
            try:
                body = (APP_DIR / "index.html").read_bytes()
            except OSError:
                self._send(
                    HTTPStatus.NOT_FOUND,
                    "text/plain; charset=utf-8",
                    b"unified app assets missing\n",
                )
            else:
                self._send(
                    HTTPStatus.OK,
                    "text/html; charset=utf-8",
                    self._with_bootstrap(body),
                )
        elif path.startswith("/static/"):
            name = Path(path[len("/static/") :]).name
            static_file = APP_DIR / name
            try:
                body = static_file.read_bytes()
            except OSError:
                self._send(
                    HTTPStatus.NOT_FOUND, "text/plain; charset=utf-8", b"not found\n"
                )
            else:
                content_type = _STATIC_TYPES.get(
                    static_file.suffix, "application/octet-stream"
                )
                self._send(HTTPStatus.OK, content_type, body)
        elif path == "/guide.md":
            try:
                body = GUIDE_FILE.read_bytes()
            except OSError:
                self._send(
                    HTTPStatus.NOT_FOUND,
                    "text/plain; charset=utf-8",
                    b"guide not found\n",
                )
            else:
                self._send(HTTPStatus.OK, "text/markdown; charset=utf-8", body)
        elif path == "/paper":
            from dashboard.paper_trading import render_paper_trading_page

            self._send(
                HTTPStatus.OK, "text/html; charset=utf-8", render_paper_trading_page()
            )
        elif path in _API_ROUTES:
            self._serve_api(path, query)
        elif path == "/strategy":
            capital = float(query.get("capital", 100_000))
            try:
                from dashboard.strategy_dashboard import render_strategy_page

                self._send(
                    HTTPStatus.OK,
                    "text/html; charset=utf-8",
                    render_strategy_page(capital),
                )
            except Exception as exc:  # noqa: BLE001 - report it, never disguise it
                # This used to quietly serve the *paper* page instead, so a
                # strategy-dashboard crash looked like a working page showing
                # unrelated content.  Fail loudly and say what failed.
                logger.exception("strategy_dashboard_unavailable")
                detail = (
                    f"{type(exc).__name__}: {exc}".replace("&", "&amp;")
                    .replace("<", "&lt;")
                    .replace(">", "&gt;")
                )
                self._send(
                    HTTPStatus.SERVICE_UNAVAILABLE,
                    "text/html; charset=utf-8",
                    (
                        "<!doctype html><meta charset='utf-8'>"
                        "<title>Strategy dashboard unavailable</title>"
                        "<body style='font-family:system-ui;background:#0d1117;"
                        "color:#e6edf3;padding:2rem;max-width:46rem'>"
                        "<h1>Strategy dashboard unavailable</h1>"
                        "<p>The signal could not be computed, so no strategy page "
                        "is being shown. Showing a different page here would hide "
                        "the failure.</p>"
                        f"<pre style='color:#f85149'>{detail}</pre>"
                        "<p>Check <a href='/operations'>Operations</a> for the "
                        "data and signal heartbeats, then try "
                        "<a href='/'>the unified dashboard</a>.</p>"
                        "</body>"
                    ).encode("utf-8"),
                )
        elif path == "/api/paper/status":
            self._send_json(HTTPStatus.OK, get_paper_service().status())
        elif path == "/api/paper/audit":
            self._send_json(HTTPStatus.OK, get_paper_service().audit())
        elif path == "/api/paper/export":
            dataset = str(query.get("dataset", "orders"))
            try:
                csv_body = get_paper_service().export_csv(dataset).encode("utf-8")
            except ValueError as exc:
                self._send_json(HTTPStatus.BAD_REQUEST, {"error": str(exc)})
            else:
                self._send_csv(dataset, csv_body)
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

    # -- access control -----------------------------------------------------

    def _with_bootstrap(self, body: bytes) -> bytes:
        """Inject the auth bootstrap into the SPA shell.

        The SPA needs the shared secret to keep its buttons working (AUDIT-013:
        a control the UI invites but the backend refuses is a broken UI). The
        secret is handed out only to a loopback client or when the operator
        explicitly opts in with ``QUANT_DASHBOARD_TOKEN_IN_UI=1``; otherwise the
        UI is told that mutations require a token so it can disable them
        instead of failing silently.
        """
        token = dashboard_token()
        if token and (self._client_is_loopback() or _token_in_ui()):
            script = (
                f"<script>window.QUANT_DASHBOARD_TOKEN={json.dumps(token)};</script>"
            )
        elif token:
            script = "<script>window.QUANT_DASHBOARD_AUTH_REQUIRED=true;</script>"
        else:
            script = ""
        if not script:
            return body
        encoded = script.encode("utf-8")
        for marker in (
            b'<script src="/static/app.js"></script>',
            b'<script src="/live/static/live_terminal.js"></script>',
            b"</head>",
        ):
            if marker in body:
                return body.replace(marker, encoded + marker, 1)
        return body + encoded

    def _client_is_loopback(self) -> bool:
        try:
            return _is_loopback(self.client_address[0])
        except (IndexError, TypeError):  # pragma: no cover - defensive
            return False

    def _authorize_mutating(self) -> bool:
        """Reject a mutating request that is not authorised.

        AUDIT-039: every route below mutates operator state. This is the single
        choke point for the origin check (CSRF) and the shared-secret header.
        Returns True when the request may proceed; on failure it has already
        written the response.
        """
        if not self._origin_allowed():
            self._send_json(
                HTTPStatus.FORBIDDEN,
                {"error": "cross-origin mutations are not allowed"},
            )
            return False
        expected = dashboard_token()
        if not expected:
            # No token configured: the deployment is trusted (loopback, or an
            # explicit ALLOW_UNAUTHENTICATED override). The origin check above
            # still applies.
            return True
        supplied = self.headers.get("X-Quant-Token", "")
        if not hmac.compare_digest(supplied, expected):
            self._send_json(HTTPStatus.UNAUTHORIZED, {"error": "unauthorized"})
            return False
        return True

    def _origin_allowed(self) -> bool:
        """Same-origin check for browser-initiated state changes."""
        origin = self.headers.get("Origin")
        if not origin:
            # Non-browser client (curl, a script). The token check is the
            # control for these; there is no ambient authority to abuse.
            return True
        try:
            from urllib.parse import urlparse

            origin_host = (urlparse(origin).hostname or "").lower()
        except ValueError:
            return False
        if not origin_host:
            return False
        host = (self.headers.get("Host", "").split(":")[0] or "").strip().lower()
        allowed = {
            host,
            "localhost",
            "127.0.0.1",
            str(os.getenv("QUANT_DASHBOARD_PUBLIC_HOST", "")).strip().lower(),
        }
        return origin_host in {value for value in allowed if value}

    def do_POST(self) -> None:  # noqa: N802
        """Handle POST requests for launching research runs."""
        path = self.path.split("?")[0]

        if not self._authorize_mutating():
            return

        if path == "/api/research/run":
            self._handle_research_run()
        elif path == "/api/live/bot":
            self._handle_live_bot()
        elif path == "/api/kill-switch":
            self._handle_kill_switch()
        elif path == "/api/signal/recompute":
            self._handle_signal_recompute()
        elif path == "/api/data/rebuild-prices":
            self._handle_rebuild_prices()
        elif path == "/api/universe/expand":
            self._handle_universe_expand()
        elif path.startswith("/api/paper/"):
            self._handle_paper_action(path)
        else:
            self._send(
                HTTPStatus.NOT_FOUND, "text/plain; charset=utf-8", b"Not found\n"
            )

    def _read_json_body(self) -> dict[str, Any]:
        """Decode a small JSON action payload; paper endpoints never accept code."""
        try:
            content_length = int(self.headers.get("Content-Length", 0))
            if content_length < 0 or content_length > 32_768:
                raise ValueError("request body is too large")
            payload = json.loads(self.rfile.read(content_length) or b"{}")
            if not isinstance(payload, dict):
                raise ValueError("JSON body must be an object")
            return payload
        except (ValueError, json.JSONDecodeError) as exc:
            raise ValueError(f"invalid JSON: {exc}") from exc

    def _handle_paper_action(self, path: str) -> None:
        """Serve explicit local virtual-paper actions only.

        None of the branches below calls an Upstox order endpoint.  Market data
        refreshes are read-only, while the other actions mutate only the local
        virtual SQLite ledger.
        """
        try:
            payload = self._read_json_body()
            paper = get_paper_service()
            if path == "/api/paper/configure":
                result = paper.configure(
                    float(payload.get("capital")),
                    str(payload.get("data_mode", "UPSTOX_DATA")),
                )
            elif path == "/api/paper/start":
                result = paper.start_monitor()
            elif path == "/api/paper/pause":
                result = paper.pause()
            elif path == "/api/paper/refresh":
                result = paper.refresh_quotes()
            elif path == "/api/paper/watchlist":
                symbols = payload.get("symbols")
                if not isinstance(symbols, list):
                    raise ValueError("symbols must be a JSON array")
                result = paper.set_watchlist([str(symbol) for symbol in symbols])
            elif path == "/api/paper/risk-policy":
                values = payload.get("policy")
                if not isinstance(values, dict):
                    raise ValueError("policy must be a JSON object")
                result = paper.set_risk_policy(values)
            elif path == "/api/paper/automation":
                result = paper.set_auto_paper(
                    enabled=bool(payload.get("enabled", False)),
                    strategy_id=str(payload.get("strategy_id", "")),
                    confirmation=str(payload.get("confirmation", "")),
                )
            elif path == "/api/paper/reset":
                raw_capital = payload.get("capital")
                result = paper.reset(
                    str(payload.get("confirmation", "")),
                    float(raw_capital) if raw_capital is not None else None,
                )
            elif path == "/api/paper/preview":
                result = paper.preview_rebalance(str(payload.get("strategy_id", "")))
            elif path == "/api/paper/rebalance":
                result = paper.execute_rebalance(
                    str(payload.get("strategy_id", "")),
                    str(payload.get("confirmation", "")),
                )
            else:
                self._send_json(HTTPStatus.NOT_FOUND, {"error": "not found"})
                return
            self._send_json(HTTPStatus.OK, result)
        except (TypeError, ValueError) as exc:
            self._send_json(HTTPStatus.BAD_REQUEST, {"error": str(exc)})
        except Exception:  # noqa: BLE001 - no internal error details in an action response
            logger.exception("paper_action_failed")
            self._send_json(
                HTTPStatus.SERVICE_UNAVAILABLE, {"error": "paper service unavailable"}
            )

    def _serve_api(self, path: str, query: dict[str, str]) -> None:
        """Serve a unified-dashboard payload as JSON (never a raw traceback)."""
        try:
            payload = _dispatch_api(path, query)
        except Exception as exc:  # noqa: BLE001
            logger.exception("api_dispatch_failed %s", path)
            payload = {"error": type(exc).__name__, "detail": str(exc)}
        self._send_json(HTTPStatus.OK, payload)

    def _serve_live_stream(self) -> None:
        """Server-Sent Events stream for the live terminal (SIM or LIVE feed)."""
        feed = get_live_feed()
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Connection", "keep-alive")
        self.send_header("X-Accel-Buffering", "no")
        self.end_headers()
        client_queue = feed.hub.add()
        try:
            handshake = f'event: hello\ndata: {{"mode": "{feed.mode}", "t": {int(time.time() * 1000)}}}\n\n'
            self.wfile.write(handshake.encode("utf-8"))
            self.wfile.flush()
            while True:
                try:
                    message = client_queue.get(timeout=20)
                except queue.Empty:
                    self.wfile.write(b": keepalive\n\n")
                    self.wfile.flush()
                    continue
                self.wfile.write(message.encode("utf-8"))
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, OSError, ValueError):
            pass
        finally:
            feed.hub.remove(client_queue)

    def _handle_kill_switch(self) -> None:
        """Arm or disarm the operator kill switch (persisted, process-wide)."""
        from datahub import state as sysstate

        try:
            payload = self._read_json_body()
            armed = bool(payload.get("armed", False))
            switch = sysstate.set_kill_switch(
                armed,
                reason=str(payload.get("reason", ""))[:200],
                armed_by=str(payload.get("by", "dashboard"))[:60],
            )
            if armed:
                # arming the switch also stops the demo trader immediately
                try:
                    get_live_feed().set_bot(False)
                except Exception:  # noqa: BLE001
                    logger.warning("kill_switch_bot_stop_failed")
            self._send_json(HTTPStatus.OK, switch)
        except ValueError as exc:
            self._send_json(HTTPStatus.BAD_REQUEST, {"error": str(exc)})
        except Exception:  # noqa: BLE001
            logger.exception("kill_switch_failed")
            self._send_json(
                HTTPStatus.SERVICE_UNAVAILABLE, {"error": "kill switch unavailable"}
            )

    def _handle_signal_recompute(self) -> None:
        """Drop the caches and recompute the strategy signal from fresh data."""
        try:
            from datahub import state as sysstate
            from datahub.panel import clear_cache, materialize_prices

            payload = self._read_json_body()
            capital = float(payload.get("capital", 100_000))
            clear_cache()
            prices = materialize_prices(force=True)
            from dashboard.strategy_dashboard import compute_momrem_signal

            signal = compute_momrem_signal(capital)
            sysstate.beat(
                "data_bundle_refreshed",
                {"prices_parquet": prices.get("size_mb"), "rows": prices.get("rows")},
            )
            self._send_json(
                HTTPStatus.OK,
                {
                    "as_of": signal["as_of"],
                    "regime": signal["regime"]["state"],
                    "basket": len(signal["basket"]),
                    "universe": signal["universe"]["size"],
                    "prices_parquet": prices,
                },
            )
        except (TypeError, ValueError) as exc:
            self._send_json(HTTPStatus.BAD_REQUEST, {"error": str(exc)})
        except Exception as exc:  # noqa: BLE001
            logger.exception("signal_recompute_failed")
            self._send_json(HTTPStatus.SERVICE_UNAVAILABLE, {"error": str(exc)})

    def _handle_rebuild_prices(self) -> None:
        """Rewrite data/clean/prices.parquet from the shared panel."""
        from datahub.panel import clear_cache, materialize_prices

        try:
            self._read_json_body()
        except ValueError:
            pass
        try:
            clear_cache()
            self._send_json(HTTPStatus.OK, materialize_prices(force=True))
        except Exception as exc:  # noqa: BLE001
            logger.exception("rebuild_prices_failed")
            self._send_json(HTTPStatus.SERVICE_UNAVAILABLE, {"error": str(exc)})

    def _handle_universe_expand(self) -> None:
        """Promote more raw NSE symbols into the broad universe cache."""
        from datahub.universe import build_broad

        try:
            payload = self._read_json_body()
            kwargs: dict[str, Any] = {}
            if payload.get("min_years") is not None:
                kwargs["min_years"] = float(payload["min_years"])
            if payload.get("min_avg_value") is not None:
                kwargs["min_avg_value"] = float(payload["min_avg_value"])
            if payload.get("limit"):
                kwargs["limit"] = int(payload["limit"])
            if payload.get("symbols"):
                symbols = payload["symbols"]
                if not isinstance(symbols, list):
                    raise ValueError("symbols must be a JSON array")
                kwargs["symbols"] = [str(x).upper() for x in symbols]
            result = build_broad(**kwargs)
            self._send_json(HTTPStatus.OK, {"result": result})
        except (TypeError, ValueError) as exc:
            self._send_json(HTTPStatus.BAD_REQUEST, {"error": str(exc)})
        except Exception as exc:  # noqa: BLE001
            logger.exception("universe_expand_failed")
            self._send_json(HTTPStatus.SERVICE_UNAVAILABLE, {"error": str(exc)})

    def _handle_live_bot(self) -> None:
        try:
            payload = self._read_json_body()
            risk = payload.get("risk_pct")
            result = get_live_feed().set_bot(
                bool(payload.get("enabled", False)),
                float(risk) if risk is not None else None,
            )
            self._send_json(HTTPStatus.OK, result)
        except (TypeError, ValueError) as exc:
            self._send_json(HTTPStatus.BAD_REQUEST, {"error": str(exc)})
        except Exception:  # noqa: BLE001
            logger.exception("live_bot_failed")
            self._send_json(
                HTTPStatus.SERVICE_UNAVAILABLE, {"error": "live feed unavailable"}
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
        # json.dumps() happily emits bare NaN/Infinity, which is not valid JSON
        # and breaks strict parsers on the other end.  Analytics produce NaN for
        # warm-up windows (a 100-day MA has no value for its first 100 bars), so
        # this would otherwise leak into almost every payload.
        try:
            body = json.dumps(
                _json_safe(data), allow_nan=False, sort_keys=True, default=str
            ).encode("utf-8")
        except ValueError:
            logger.exception("json_serialisation_failed")
            body = json.dumps(
                {"error": "payload contained non-finite values"}, sort_keys=True
            ).encode("utf-8")
        self._send(code, "application/json", body)

    def _send_csv(self, dataset: str, body: bytes) -> None:
        safe_name = "".join(
            character
            for character in dataset.lower()
            if character.isalnum() or character == "_"
        )
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "text/csv; charset=utf-8")
        self.send_header(
            "Content-Disposition",
            f'attachment; filename="paper_{safe_name or "export"}.csv"',
        )
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _send(self, code: HTTPStatus, content_type: str, body: bytes) -> None:
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)


def run_server(port: int | None = None, *, bind: str | None = None) -> None:
    """Run the dashboard and local 30-second quote poller.

    AUDIT-039: the bind address defaults to **loopback**. Listening on a
    routable interface is an explicit opt-in (``QUANT_DASHBOARD_BIND``) and,
    without ``QUANT_DASHBOARD_TOKEN``, is refused outright — see
    :func:`resolve_bind`.
    """
    from paper_trading.poller import PaperQuotePoller

    actual_port = port if port is not None else int(os.getenv("PORT", "8080"))
    # Resolve the bind *first*: a refused configuration must not start the
    # quote poller or bind a socket on the way out.
    actual_bind = resolve_bind(bind)
    paper = get_paper_service()
    poller = PaperQuotePoller(paper, interval_seconds=paper.quote_stale_seconds)
    poller.start()
    # nosec B104: the address is resolved by resolve_bind(), which refuses a
    # routable bind unless a shared secret is configured.
    server = ThreadingHTTPServer((actual_bind, actual_port), DashboardHandler)  # nosec B104
    host = "localhost" if _is_loopback(actual_bind) else actual_bind
    print(f"Quant India unified dashboard: http://{host}:{actual_port}/")
    print(f"  bind          {actual_bind}:{actual_port}")
    print(
        "  mutations     "
        + (
            "shared secret required (X-Quant-Token)"
            if dashboard_token()
            else "unauthenticated (loopback bind)"
        )
    )
    print(f"  ├─ strategy    http://{host}:{actual_port}/strategy")
    print(f"  ├─ live        http://{host}:{actual_port}/live")
    print(f"  ├─ paper       http://{host}:{actual_port}/paper")
    print(f"  ├─ research    http://{host}:{actual_port}/cockpit")
    print(f"  └─ operations  http://{host}:{actual_port}/operations")
    try:
        server.serve_forever()
    finally:
        poller.stop()
        server.server_close()


if __name__ == "__main__":
    run_server()
