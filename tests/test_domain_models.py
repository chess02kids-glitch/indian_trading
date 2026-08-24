"""Phase 1 tests: core domain model validation."""

from __future__ import annotations

from datetime import UTC, date, datetime

import pytest
from pydantic import ValidationError

from models.domain import (
    MarketBar,
    OrderIntent,
    OrderSide,
    PortfolioTarget,
    Position,
    ResearchResult,
    RiskDecision,
)

NOW = datetime(2026, 8, 24, tzinfo=UTC)


class TestMarketBar:
    def _bar(self, **overrides) -> dict:
        base = {
            "source": "synthetic",
            "symbol": "RELIANCE",
            "exchange": "nse",
            "date": date(2026, 8, 21),
            "open": 100.0,
            "high": 105.0,
            "low": 99.0,
            "close": 104.0,
            "volume": 1_000_000,
        }
        base.update(overrides)
        return base

    def test_valid_bar(self) -> None:
        bar = MarketBar.model_validate(self._bar())
        assert bar.exchange == "NSE"

    def test_high_below_open_close_rejected(self) -> None:
        with pytest.raises(ValidationError, match="high"):
            MarketBar.model_validate(self._bar(high=98.0))

    def test_low_above_open_close_rejected(self) -> None:
        with pytest.raises(ValidationError, match="low"):
            MarketBar.model_validate(self._bar(low=101.0))

    def test_high_below_low_rejected(self) -> None:
        # high < low cannot coexist with low <= min(open, close), so the
        # first violated invariant reported is the low-vs-open/close check.
        with pytest.raises(ValidationError, match="low must be <= min"):
            MarketBar.model_validate(
                self._bar(open=100.0, high=101.0, low=102.0, close=100.5)
            )

    def test_non_positive_prices_rejected(self) -> None:
        for field_name in ("open", "high", "low", "close"):
            with pytest.raises(ValidationError):
                MarketBar.model_validate(self._bar(**{field_name: 0.0}))

    def test_negative_volume_rejected(self) -> None:
        with pytest.raises(ValidationError):
            MarketBar.model_validate(self._bar(volume=-1))

    def test_unknown_exchange_rejected(self) -> None:
        with pytest.raises(ValidationError):
            MarketBar.model_validate(self._bar(exchange="CME"))

    def test_adjusted_bar_requires_adj_close(self) -> None:
        with pytest.raises(ValidationError, match="adj_close"):
            MarketBar.model_validate(self._bar(is_adjusted=True))

    def test_adj_close_sets_is_adjusted(self) -> None:
        bar = MarketBar.model_validate(self._bar(adj_close=98.0))
        assert bar.is_adjusted is True


class TestPortfolioTarget:
    def test_valid_target(self) -> None:
        target = PortfolioTarget.model_validate(
            {
                "strategy_id": "s",
                "hypothesis_id": "HYP-00001",
                "as_of": date(2026, 8, 24),
                "limits": {"reliance": 100.0, "tcs": 4000.0},
                "target_quantities": {"RELIANCE": 10},
            }
        )
        assert target.limits == {"RELIANCE": 100.0, "TCS": 4000.0}

    def test_negative_limit_rejected(self) -> None:
        with pytest.raises(ValidationError):
            PortfolioTarget.model_validate(
                {
                    "strategy_id": "s",
                    "hypothesis_id": "h",
                    "as_of": date(2026, 8, 24),
                    "limits": {"A": -5.0},
                }
            )

    def test_negative_target_quantity_rejected(self) -> None:
        with pytest.raises(ValidationError):
            PortfolioTarget.model_validate(
                {
                    "strategy_id": "s",
                    "hypothesis_id": "h",
                    "as_of": date(2026, 8, 24),
                    "limits": {"A": 1.0},
                    "target_quantities": {"A": -1},
                }
            )


class TestOtherModels:
    def test_position_requires_whole_shares(self) -> None:
        with pytest.raises(ValidationError):
            Position(symbol="A", quantity=1.5)

    def test_risk_decision_serializes(self) -> None:
        decision = RiskDecision(
            state="LOCK_ACCOUNT",
            triggered_by=("reconciliation_lock",),
            details={"reason": "mismatch"},
            human_action_required=True,
            timestamp=NOW,
        )
        assert decision.state == "LOCK_ACCOUNT"

    def test_research_result_requires_finite_metrics(self) -> None:
        with pytest.raises(ValidationError):
            ResearchResult(
                hypothesis_id="h",
                strategy_id="s",
                status="accepted",
                metrics={"sharpe": float("nan")},
            )

    def test_order_intent_rejects_market_string(self) -> None:
        with pytest.raises(ValidationError):
            OrderIntent(
                internal_order_id="o",
                idempotency_key="k",
                strategy_id="s",
                hypothesis_id="h",
                symbol="RELIANCE",
                side=OrderSide.BUY,
                quantity=1,
                limit_price=1.0,
                order_type="MARKET",  # type: ignore[arg-type]
                timestamp=NOW,
            )
