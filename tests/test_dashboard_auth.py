"""Regressions for AUDIT-039 — unauthenticated dashboard mutations.

The dashboard can arm **and disarm** the kill switch, reset the paper
account and trigger rebalances. Before the fix it bound ``0.0.0.0`` with
no authentication, authorisation, CSRF or origin check, so any host that
could reach the port could disarm the kill switch with one ``curl``.

These tests pin the four controls that replaced it:

1. loopback default bind,
2. refusal to bind a routable address without a shared secret,
3. a same-origin check on every mutating request (CSRF),
4. an ``X-Quant-Token`` check on every mutating route,

and, most importantly, that **no** POST route can mutate state without
those checks.
"""

from __future__ import annotations

import json
import os
import threading
from http.client import HTTPConnection
from http.server import ThreadingHTTPServer

import pytest

from dashboard.server import (
    ALLOW_INSECURE_ENV,
    BIND_ENV,
    TOKEN_ENV,
    TOKEN_IN_UI_ENV,
    DashboardAccessError,
    DashboardHandler,
    resolve_bind,
)

# Every route that mutates operator state. This list must stay in step with
# ``DashboardHandler.do_POST`` — ``test_every_mutating_route_is_listed`` fails
# if a new route is added without being added here.
MUTATING_ROUTES = (
    "/api/research/run",
    "/api/live/bot",
    "/api/kill-switch",
    "/api/signal/recompute",
    "/api/data/rebuild-prices",
    "/api/universe/expand",
    "/api/paper/configure",
    "/api/paper/rebalance",
    "/api/paper/auto",
    "/api/paper/watchlist",
    "/api/paper/risk-policy",
    "/api/paper/monitor",
)

TEST_TOKEN = "dashboard-audit-token"


@pytest.fixture(autouse=True)
def _clean_dashboard_env(monkeypatch):
    """Remove every dashboard access-control variable between tests."""
    for name in (TOKEN_ENV, BIND_ENV, ALLOW_INSECURE_ENV, TOKEN_IN_UI_ENV):
        monkeypatch.delenv(name, raising=False)
    yield


@pytest.fixture
def server():
    """Start the real dashboard handler on a loopback port."""
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), DashboardHandler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        yield httpd.server_address[1], httpd.server_address[0]
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join(timeout=5)


def _request(
    method: str,
    path: str,
    *,
    port: int,
    host: str = "127.0.0.1",
    body: dict | None = None,
    headers: dict[str, str] | None = None,
):
    conn = HTTPConnection(host, port, timeout=10)
    payload = json.dumps(body or {}).encode()
    sent = dict(headers or {})
    sent.setdefault("Content-Type", "application/json")
    sent.setdefault("Content-Length", str(len(payload)))
    conn.request(method, path, body=payload, headers=sent)
    response = conn.getresponse()
    raw = response.read()
    conn.close()
    return response.status, raw


# ---------------------------------------------------------------------------
# 1/2. Bind resolution
# ---------------------------------------------------------------------------


class TestResolveBind:
    """The server must refuse an unsafe bind before it ever listens."""

    def test_defaults_to_loopback(self):
        assert resolve_bind(None) == "127.0.0.1"

    def test_loopback_bind_needs_no_token(self, monkeypatch):
        monkeypatch.setenv(BIND_ENV, "127.0.0.1")
        assert resolve_bind() == "127.0.0.1"

    def test_routable_bind_without_token_is_refused(self, monkeypatch):
        monkeypatch.setenv(BIND_ENV, "0.0.0.0")
        with pytest.raises(DashboardAccessError) as excinfo:
            resolve_bind()
        message = str(excinfo.value)
        assert TOKEN_ENV in message
        assert "0.0.0.0" in message
        assert "kill-switch" in message

    def test_routable_bind_with_token_is_allowed(self, monkeypatch):
        monkeypatch.setenv(BIND_ENV, "0.0.0.0")
        monkeypatch.setenv(TOKEN_ENV, TEST_TOKEN)
        assert resolve_bind() == "0.0.0.0"

    def test_routable_bind_with_explicit_override_is_allowed(self, monkeypatch):
        monkeypatch.setenv(BIND_ENV, "0.0.0.0")
        monkeypatch.setenv(ALLOW_INSECURE_ENV, "1")
        assert resolve_bind() == "0.0.0.0"

    @pytest.mark.parametrize(
        "address", ["192.168.1.10", "10.0.0.5", "172.16.0.9", "8.8.8.8"]
    )
    def test_private_and_public_addresses_are_not_loopback(self, address):
        assert not resolve_bind.__globals__["_is_loopback"](address)

    @pytest.mark.parametrize("address", ["127.0.0.1", "::1", "localhost"])
    def test_loopback_addresses(self, address):
        assert resolve_bind.__globals__["_is_loopback"](address)

    def test_unresolvable_hostname_is_treated_as_routable(self):
        # Fail closed: an unknown name must not be assumed to be loopback.
        assert not resolve_bind.__globals__["_is_loopback"]("host.example.internal")


# ---------------------------------------------------------------------------
# 3/4. Request authorisation
# ---------------------------------------------------------------------------


class TestMutatingRoutesRequireAuthorisation:
    """No POST route may mutate state without the origin and token checks."""

    def test_unauthenticated_kill_switch_write_is_rejected(self, server, monkeypatch):
        port, _ = server
        monkeypatch.setenv(TOKEN_ENV, TEST_TOKEN)
        status, raw = _request(
            "POST",
            "/api/kill-switch",
            port=port,
            body={"armed": True, "reason": "unauthenticated"},
        )
        assert status == 401
        assert b"unauthorized" in raw.lower()

    def test_unauthenticated_kill_switch_does_not_change_state(
        self, server, monkeypatch
    ):
        """The 401 must be written before any handler runs, not after."""
        from datahub import kill_switch as kill

        port, _ = server
        monkeypatch.setenv(TOKEN_ENV, TEST_TOKEN)
        assert not kill.is_killed()
        status, _ = _request(
            "POST",
            "/api/kill-switch",
            port=port,
            body={"armed": True, "reason": "unauthenticated"},
        )
        assert status == 401
        assert not kill.is_killed()

    def test_authenticated_kill_switch_write_is_applied(self, server, monkeypatch):
        from datahub import kill_switch as kill

        port, _ = server
        monkeypatch.setenv(TOKEN_ENV, TEST_TOKEN)
        try:
            status, raw = _request(
                "POST",
                "/api/kill-switch",
                port=port,
                body={"armed": True, "reason": "audit-039 test"},
                headers={"X-Quant-Token": TEST_TOKEN},
            )
            assert status == 200
            assert json.loads(raw)["armed"] is True
            assert kill.is_killed()
        finally:
            kill.clear_risk_state() if hasattr(kill, "clear_risk_state") else None
            from datahub import state as sysstate

            sysstate.set_kill_switch(False)

    def test_wrong_token_is_rejected(self, server, monkeypatch):
        port, _ = server
        monkeypatch.setenv(TOKEN_ENV, TEST_TOKEN)
        status, _ = _request(
            "POST",
            "/api/kill-switch",
            port=port,
            body={"armed": False},
            headers={"X-Quant-Token": "not-the-token"},
        )
        assert status == 401

    def test_cross_origin_mutation_is_rejected(self, server, monkeypatch):
        """CSRF: a browser on a hostile page must not reach these routes."""
        port, host = server
        monkeypatch.setenv(TOKEN_ENV, TEST_TOKEN)
        status, raw = _request(
            "POST",
            "/api/kill-switch",
            port=port,
            host=host,
            body={"armed": True},
            headers={
                "Origin": "https://evil.example",
                "Host": f"{host}:{port}",
                "X-Quant-Token": TEST_TOKEN,
            },
        )
        assert status == 403
        assert b"cross-origin" in raw.lower()

    def test_same_origin_mutation_is_allowed(self, server, monkeypatch):
        port, host = server
        monkeypatch.setenv(TOKEN_ENV, TEST_TOKEN)
        from datahub import state as sysstate

        try:
            status, _ = _request(
                "POST",
                "/api/kill-switch",
                port=port,
                host=host,
                body={"armed": True, "reason": "same-origin audit test"},
                headers={
                    "Origin": f"http://{host}:{port}",
                    "Host": f"{host}:{port}",
                    "X-Quant-Token": TEST_TOKEN,
                },
            )
            assert status == 200
        finally:
            sysstate.set_kill_switch(False)

    @pytest.mark.parametrize("path", MUTATING_ROUTES)
    def test_every_mutating_route_is_rejected_without_a_token(
        self, server, monkeypatch, path
    ):
        """The auth choke point must sit in front of *every* POST route."""
        port, _ = server
        monkeypatch.setenv(TOKEN_ENV, TEST_TOKEN)
        status, _ = _request("POST", path, port=port, body={})
        assert status == 401, f"{path} mutated state without a token"

    def test_no_token_configured_still_enforces_origin(self, server):
        """Loopback deployments are trusted, but CSRF is still blocked."""
        port, host = server
        status, _ = _request(
            "POST",
            "/api/kill-switch",
            port=port,
            host=host,
            body={"armed": True},
            headers={"Origin": "https://evil.example", "Host": f"{host}:{port}"},
        )
        assert status == 403


def test_do_post_route_list_matches_this_module():
    """If a new mutating route is added, this file must learn about it."""
    import inspect
    import re

    source = inspect.getsource(DashboardHandler.do_POST)
    dispatched = set(re.findall(r'path\s*==\s*"([^"]+)"', source))
    dispatched |= set(re.findall(r'path\.startswith\("([^"]+)"\)', source))
    declared = set(MUTATING_ROUTES)
    # Paper routes share one prefix; the concrete ones are still enumerated.
    missing = {
        route
        for route in dispatched
        if route not in declared and not route.startswith("/api/paper/")
    }
    assert not missing, f"new mutating routes are untested: {sorted(missing)}"


# ---------------------------------------------------------------------------
# Token handling in the served UI
# ---------------------------------------------------------------------------


class TestTokenIsNotLeakedToThePage:
    """The shared secret must not be handed to a non-loopback client."""

    def test_loopback_client_receives_the_token(self, server, monkeypatch):
        port, host = server
        monkeypatch.setenv(TOKEN_ENV, TEST_TOKEN)
        status, raw = _request("GET", "/", port=port, host=host)
        assert status == 200
        assert b"QUANT_DASHBOARD_TOKEN" in raw
        assert TEST_TOKEN.encode() in raw

    def test_non_loopback_client_does_not(self, server, monkeypatch):
        """A remote client must not be handed the secret by default.

        The socket is loopback in this test, so ``_client_is_loopback`` is
        patched to report what a genuinely remote client would report.
        """
        port, _ = server
        monkeypatch.setenv(TOKEN_ENV, TEST_TOKEN)
        monkeypatch.setenv(
            "QUANT_DASHBOARD_PUBLIC_HOST", "dashboard.internal.example"
        )
        monkeypatch.setattr(
            DashboardHandler, "_client_is_loopback", lambda self: False
        )
        status, raw = _request("GET", "/", port=port)
        assert status == 200
        assert TEST_TOKEN.encode() not in raw
        assert b"QUANT_DASHBOARD_AUTH_REQUIRED" in raw

    def test_opt_in_ui_token_flag_is_respected(self, server, monkeypatch):
        port, host = server
        monkeypatch.setenv(TOKEN_ENV, TEST_TOKEN)
        monkeypatch.setenv(TOKEN_IN_UI_ENV, "1")
        status, raw = _request("GET", "/", port=port, host=host)
        assert status == 200
        assert TEST_TOKEN.encode() in raw

    def test_the_ui_is_told_mutations_need_a_token(self, server, monkeypatch):
        """The SPA disables its controls instead of failing silently."""
        port, host = server
        monkeypatch.setenv(TOKEN_ENV, TEST_TOKEN)
        monkeypatch.setattr(
            DashboardHandler, "_client_is_loopback", lambda self: False
        )
        status, raw = _request("GET", "/", port=port, host=host)
        assert status == 200
        assert b"QUANT_DASHBOARD_AUTH_REQUIRED" in raw


def test_run_server_refuses_an_unsafe_bind(monkeypatch):
    """``run_server()`` consults ``resolve_bind`` before it starts anything."""
    import dashboard.server as server_module
    from paper_trading import poller as poller_module

    started: list[object] = []

    class _FakePoller:
        def start(self):
            started.append("poller")

        def stop(self):
            pass

    monkeypatch.setattr(poller_module, "PaperQuotePoller", lambda *a, **kw: _FakePoller())
    monkeypatch.setattr(server_module, "get_paper_service", lambda: object())
    monkeypatch.setenv(BIND_ENV, "0.0.0.0")
    monkeypatch.delenv(TOKEN_ENV, raising=False)
    monkeypatch.delenv(ALLOW_INSECURE_ENV, raising=False)

    with pytest.raises(DashboardAccessError):
        server_module.run_server(port=0, bind="0.0.0.0")

    # The refusal must happen before side effects, not after them.
    assert started == []


def test_environment_variables_are_documented():
    """The runbook must tell an operator how to deploy this safely."""
    runbook = os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        "docs",
        "local_paper_trading.md",
    )
    text = open(runbook, encoding="utf-8").read()
    for name in (TOKEN_ENV, BIND_ENV, ALLOW_INSECURE_ENV):
        assert name in text, f"{name} is undocumented"
