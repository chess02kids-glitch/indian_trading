"""Upstox/Dhan sandbox adapters: unified interface, dialects, interchangeability."""

from __future__ import annotations

from datetime import timedelta

import pytest

from broker.adapter import BaseSandboxAdapter, create_adapter
from broker.errors import (
    BrokerConfigurationError,
    BrokerRejectedOrderError,
    LiveTradingDisabledError,
    SandboxOnlyError,
    StaleTokenError,
)
from broker.mode import OperatingMode
from broker.simulated import PendingFault
from broker.token import FileTokenStore, TokenManager
from broker.transport import (
    HttpSandboxTransportStub,
    SimulatedSandboxTransport,
    validate_sandbox_base_url,
)
from models.domain import OrderSide, OrderStatus
from tests.sandbox_common import FakeClock, SandboxEnv, make_intent


class TestFactoryAndModes:
    def test_factory_builds_both_brokers(self, tmp_path) -> None:
        for name in ("upstox", "dhan"):
            env = SandboxEnv(tmp_path / name, name)
            assert isinstance(env.adapter, BaseSandboxAdapter)
            assert env.adapter.broker_name == name

    def test_factory_rejects_unknown_broker(self) -> None:
        with pytest.raises(BrokerConfigurationError, match="unsupported"):
            create_adapter("zerodha")

    def test_adapter_refuses_live_mode(self, tmp_path) -> None:
        with pytest.raises(LiveTradingDisabledError):
            create_adapter(
                "upstox",
                transport=SimulatedSandboxTransport(
                    "upstox", state_path=tmp_path / "s.json"
                ),
                token_manager=TokenManager(FileTokenStore(tmp_path / "t")),
                mode=OperatingMode.LIVE,
            )

    def test_adapter_refuses_non_sandbox_mode(self, tmp_path) -> None:
        with pytest.raises(SandboxOnlyError):
            create_adapter(
                "upstox",
                transport=SimulatedSandboxTransport(
                    "upstox", state_path=tmp_path / "s.json"
                ),
                token_manager=TokenManager(FileTokenStore(tmp_path / "t")),
                mode=OperatingMode.PAPER,
            )


class TestBaseUrlPolicy:
    @pytest.mark.parametrize(
        "url",
        [
            "https://api.upstox.com/v2",
            "https://api.dhan.co/v2",
            "https://production.exchange.example",
        ],
    )
    def test_production_urls_refused(self, url: str) -> None:
        with pytest.raises(LiveTradingDisabledError):
            validate_sandbox_base_url(url)

    @pytest.mark.parametrize(
        "url",
        [
            "simulated://upstox",
            "https://sandbox.upstox.com",
            "https://api-sandbox.dhan.co",
            "http://localhost:9000",
            "http://127.0.0.1:9000",
        ],
    )
    def test_sandbox_urls_permitted(self, url: str) -> None:
        assert validate_sandbox_base_url(url) == url

    def test_http_stub_is_inert(self) -> None:
        stub = HttpSandboxTransportStub("https://sandbox.upstox.com")
        with pytest.raises(Exception, match="offline build"):
            stub.request("profile")

    def test_http_stub_rejects_simulated_scheme(self) -> None:
        with pytest.raises(BrokerConfigurationError):
            HttpSandboxTransportStub("simulated://upstox")


class TestAuthentication:
    def test_unauthenticated_reads_raise_stale_token(self, tmp_path) -> None:
        env = SandboxEnv(tmp_path, "upstox")
        with pytest.raises(StaleTokenError):
            env.adapter.get_funds()

    def test_login_url_contains_state(self, tmp_path) -> None:
        env = SandboxEnv(tmp_path, "dhan")
        url = env.adapter.login_url("xyz-1")
        assert "state=xyz-1" in url
        assert url.startswith("simulated://dhan")

    def test_login_url_rejects_empty_state(self, tmp_path) -> None:
        env = SandboxEnv(tmp_path, "upstox")
        with pytest.raises(BrokerConfigurationError):
            env.adapter.login_url("  ")

    def test_complete_login_persists_token(self, tmp_path) -> None:
        env = SandboxEnv(tmp_path, "upstox")
        token = env.login()
        assert env.adapter.is_authenticated()
        assert env.adapter.token_manager.get_token("upstox") == token

    def test_login_empty_code_rejected(self, tmp_path) -> None:
        env = SandboxEnv(tmp_path, "dhan")
        with pytest.raises(Exception, match="code"):
            env.adapter.complete_login("  ")

    def test_rehydrated_session_survives_new_adapter(self, tmp_path) -> None:
        """A stored sandbox token is re-registered with a fresh backend."""
        env = SandboxEnv(tmp_path, "upstox")
        env.login()
        clock = env.clock
        from broker.adapter import create_adapter as factory

        transport = SimulatedSandboxTransport(
            "upstox", state_path=tmp_path / "fresh.json", clock=clock
        )
        rehydrated = factory("upstox", transport=transport, token_manager=env.tokens)
        funds = rehydrated.get_funds()
        assert funds.available_cash == pytest.approx(1_000_000.0)


class TestAccountReads:
    @pytest.fixture(params=["upstox", "dhan"])
    def env(self, request, tmp_path):
        sandbox = SandboxEnv(tmp_path, request.param)
        sandbox.login()
        return sandbox

    def test_ping(self, env) -> None:
        assert env.adapter.ping()

    def test_profile(self, env) -> None:
        profile = env.adapter.get_profile()
        assert profile.broker == env.broker
        assert profile.user_name == "Sandbox Operator"
        assert "NSE" in profile.exchanges

    def test_funds_initial(self, env) -> None:
        funds = env.adapter.get_funds()
        assert funds.available_cash == pytest.approx(1_000_000.0)
        assert funds.currency == "INR"

    def test_holdings_and_positions_start_empty(self, env) -> None:
        assert env.adapter.get_holdings() == []
        assert env.adapter.get_positions() == []

    def test_quote_is_deterministic_and_positive(self, env) -> None:
        first = env.adapter.get_quote("RELIANCE")
        second = env.adapter.get_quote("RELIANCE")
        assert first.last_price > 0
        assert first.last_price == second.last_price
        assert first.symbol == "RELIANCE"

    def test_token_expiry_blocks_reads(self, tmp_path) -> None:
        clock = FakeClock()
        env = SandboxEnv(tmp_path, "upstox", clock=clock, token_ttl_hours=1.0)
        env.login()
        clock.advance(timedelta(hours=2))
        with pytest.raises(StaleTokenError):
            env.adapter.get_profile()
        assert not env.adapter.is_authenticated()


class TestOrderPlacement:
    @pytest.fixture(params=["upstox", "dhan"])
    def env(self, request, tmp_path):
        sandbox = SandboxEnv(tmp_path, request.param)
        sandbox.login()
        return sandbox

    def test_buy_fills_and_moves_cash_and_position(self, env) -> None:
        record = env.adapter.place_limit_order(make_intent(quantity=10, price=100.0))
        assert record.status is OrderStatus.FILLED
        assert record.filled_quantity == 10
        assert record.order_id.startswith(f"{env.broker}-sbx-")
        funds = env.adapter.get_funds()
        assert funds.available_cash == pytest.approx(1_000_000.0 - 1000.0)
        positions = env.adapter.get_positions()
        assert [(p.symbol, p.quantity) for p in positions] == [("RELIANCE", 10)]

    def test_second_buy_weights_average_price(self, env) -> None:
        env.adapter.place_limit_order(
            make_intent("ord-1", quantity=10, price=100.0, rebalance_date="2026-08-25")
        )
        env.adapter.place_limit_order(
            make_intent("ord-2", quantity=10, price=110.0, rebalance_date="2026-08-26")
        )
        position = env.adapter.get_positions()[0]
        assert position.quantity == 20
        assert position.average_price == pytest.approx(105.0)

    def test_sell_requires_holdings(self, env) -> None:
        record = env.adapter.place_limit_order(
            make_intent(side=OrderSide.SELL, quantity=5, price=100.0)
        )
        assert record.status is OrderStatus.REJECTED
        assert "holdings" in (record.message or "")

    def test_sell_after_buy_moves_cash_back(self, env) -> None:
        env.adapter.place_limit_order(make_intent("ord-1", quantity=10, price=100.0))
        record = env.adapter.place_limit_order(
            make_intent(
                "ord-2",
                side=OrderSide.SELL,
                quantity=4,
                price=100.0,
                rebalance_date="2026-08-26",
            )
        )
        assert record.status is OrderStatus.FILLED
        funds = env.adapter.get_funds()
        assert funds.available_cash == pytest.approx(1_000_000.0 - 600.0)
        assert env.adapter.get_positions()[0].quantity == 6

    def test_insufficient_funds_rejected(self, env) -> None:
        record = env.adapter.place_limit_order(
            make_intent(quantity=50_000, price=100.0)
        )
        assert record.status is OrderStatus.REJECTED
        assert "funds" in (record.message or "")

    def test_non_limit_wire_order_rejected(self, env) -> None:
        """Broker-side LIMIT guard: the backend itself refuses MARKET/IOC."""
        with pytest.raises(BrokerRejectedOrderError):
            env.backend().place_order(
                {
                    "tag": "x",
                    "symbol": "RELIANCE",
                    "side": "BUY",
                    "quantity": 1,
                    "price": 100.0,
                    "order_type": "MARKET",
                },
                token=env.adapter.token_manager.get_token(env.broker),
            )

    def test_duplicate_tag_returns_original_order(self, env) -> None:
        """Broker-side dedup: the same client order id never creates two orders."""
        first = env.adapter.place_limit_order(make_intent("ord-1"))
        second = env.adapter.place_limit_order(make_intent("ord-1"))
        assert second.duplicate is True
        assert second.order_id == first.order_id
        orders = env.backend().list_orders(
            token=env.adapter.token_manager.get_token(env.broker)
        )
        assert len(orders) == 1
        assert len(env.adapter.get_trade_history()) == 1

    def test_idempotency_key_carried_through(self, env) -> None:
        intent = make_intent("ord-1")
        record = env.adapter.place_limit_order(intent)
        assert record.tag == "ord-1"
        assert record.idempotency_key == intent.idempotency_key

    def test_order_status_by_id_and_tag(self, env) -> None:
        record = env.adapter.place_limit_order(make_intent("ord-1"))
        by_id = env.adapter.get_order_status(record.order_id)
        by_tag = env.adapter.get_order_status(record.tag or "")
        assert by_id is not None and by_tag is not None
        assert by_id.order_id == by_tag.order_id

    def test_get_order_status_unknown_returns_none(self, env) -> None:
        assert env.adapter.get_order_status("nope") is None

    def test_cancel_pending_and_filled(self, env) -> None:
        env.transport.script("place", [PendingFault(polls=2)])
        pending = env.adapter.place_limit_order(make_intent("ord-1"))
        assert pending.status is OrderStatus.PENDING
        cancelled = env.adapter.cancel_order(pending.order_id)
        assert cancelled is not None and cancelled.status is OrderStatus.CANCELLED

        filled = env.adapter.place_limit_order(
            make_intent("ord-2", symbol="TCS", rebalance_date="2026-08-26")
        )
        closed = env.adapter.cancel_order(filled.order_id)
        assert closed is not None and closed.status is OrderStatus.FILLED
        assert "already closed" in (closed.message or "")

    def test_cancel_unknown_returns_none(self, env) -> None:
        assert env.adapter.cancel_order("ghost") is None

    def test_trade_history_records_fills(self, env) -> None:
        env.adapter.place_limit_order(make_intent("ord-1", quantity=3, price=100.0))
        trades = env.adapter.get_trade_history()
        assert len(trades) == 1
        assert trades[0].symbol == "RELIANCE"
        assert trades[0].quantity == 3

    def test_holdings_mirror_positions(self, env) -> None:
        env.adapter.place_limit_order(make_intent("ord-1", quantity=3, price=100.0))
        holdings = env.adapter.get_holdings()
        assert [(h.symbol, h.quantity) for h in holdings] == [("RELIANCE", 3)]


class TestDialectsAndInterchangeability:
    def test_wire_dialects_differ_domains_match(self, tmp_path) -> None:
        """Both brokers fill identically at the domain level; wire differs."""
        outcomes = {}
        for name in ("upstox", "dhan"):
            env = SandboxEnv(tmp_path / name, name)
            env.login()
            record = env.adapter.place_limit_order(make_intent("ord-1"))
            outcomes[name] = record
        upstox, dhan = outcomes["upstox"], outcomes["dhan"]
        assert upstox.status == dhan.status is OrderStatus.FILLED
        assert upstox.filled_quantity == dhan.filled_quantity == 10
        assert upstox.tag == dhan.tag
        # wire dialects genuinely differ
        assert upstox.raw_status == "complete"
        assert dhan.raw_status == "traded"
        assert upstox.order_id.startswith("upstox-")
        assert dhan.order_id.startswith("dhan-")

    def test_pending_dialects(self, tmp_path) -> None:
        raws = {}
        for name in ("upstox", "dhan"):
            env = SandboxEnv(tmp_path / name, name)
            env.login()
            env.transport.script("place", [PendingFault(polls=1)])
            record = env.adapter.place_limit_order(make_intent("ord-1"))
            raws[name] = (record.status, record.raw_status)
        assert raws["upstox"] == (OrderStatus.PENDING, "open")
        assert raws["dhan"] == (OrderStatus.PENDING, "pending")


class TestPersistence:
    def test_backend_state_survives_restart(self, tmp_path) -> None:
        env = SandboxEnv(tmp_path, "upstox")
        token = env.login()
        env.adapter.place_limit_order(make_intent("ord-1", quantity=7, price=100.0))

        # "restart": brand new transport+adapter over the same state file
        restarted = create_adapter(
            "upstox",
            transport=SimulatedSandboxTransport(
                "upstox", state_path=env.state_path, clock=env.clock
            ),
            token_manager=env.tokens,
        )
        funds = restarted.get_funds()
        assert funds.available_cash == pytest.approx(1_000_000.0 - 700.0)
        positions = restarted.get_positions()
        assert [(p.symbol, p.quantity) for p in positions] == [("RELIANCE", 7)]
        status = restarted.get_order_status("upstox-sbx-00000001")
        assert status is not None and status.status is OrderStatus.FILLED
        assert restarted.token_manager.get_token("upstox") == token

    def test_state_file_broker_mismatch_guard(self, tmp_path) -> None:
        env = SandboxEnv(tmp_path, "upstox")
        env.login()
        with pytest.raises(Exception, match="belongs"):
            SimulatedSandboxTransport(
                "dhan", state_path=env.state_path, clock=env.clock
            )


class TestLiveModeRefusalPaths:
    def test_tampered_mode_refuses_submission(self, tmp_path) -> None:
        """If an adapter is ever flipped to LIVE, submission refuses hard."""
        env = SandboxEnv(tmp_path, "upstox")
        env.login()
        env.adapter._mode = OperatingMode.LIVE
        with pytest.raises(LiveTradingDisabledError):
            env.adapter.place_limit_order(make_intent("ord-1"))
