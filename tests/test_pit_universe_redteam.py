"""PIT universe red-team: synthetic membership scenarios against the zoo.

Scenarios: company joins the universe, exits, delists, changes symbol,
IPOs after the panel start, is absent from the historical dataset, and
overlaps a corporate action. For every scenario the invariant is: at any
selection date, selected ⊆ members (mask-before-rank), and the machinery
either handles the case or raises a clear error — never silently wrong.
"""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from research.contracts import MarketData, ResearchInputError
from research.leakage import audit_rank_mask_order
from research.synthetic_worlds import SyntheticWorld
from research.zoo import ZOO_FAMILIES, run_benchmark_zoo


def make_panel(
    n_symbols: int = 8,
    n_days: int = 420,
    seed: int = 5,
) -> pd.DataFrame:
    generator = np.random.default_rng(seed)
    index = pd.bdate_range("2024-01-02", periods=n_days)
    columns = [f"T{i:02d}" for i in range(n_symbols)]
    returns = generator.normal(0.0004, 0.015, size=(n_days, n_symbols))
    return pd.DataFrame(
        100.0 * np.exp(np.cumsum(returns, axis=0)), index=index, columns=columns
    )


def month_ends(frame: pd.DataFrame) -> pd.DataFrame:
    return frame[~frame.index.to_period("M").duplicated(keep="last")]


def assert_pit_invariant(world: SyntheticWorld) -> None:
    """Run the zoo and assert mask-before-rank at every selection date."""
    results = run_benchmark_zoo(
        world.market_data.close,
        fundamentals=world.fundamentals,
        membership=world.membership,
    )
    for family_id in (
        "cross_sectional_momentum",
        "low_volatility",
        "mean_reversion",
        "trend_following",
    ):
        weights = results[family_id].weights
        audit = audit_rank_mask_order(
            weights,
            world.membership,
            selection_dates=month_ends(weights).index,
        )
        assert audit["clean"] is True, (
            f"{family_id}: {audit['violations']} violations, "
            f"{len(audit['order_violations'])} order violations"
        )


def world_with_membership(
    membership: pd.DataFrame, prices: pd.DataFrame
) -> SyntheticWorld:
    return SyntheticWorld(
        world_id="T",
        name="pit_scenario",
        description="PIT universe red-team scenario",
        truth={"signal": "none"},
        market_data=MarketData(close=prices),
        seed=1,
        membership=membership,
    )


class TestMembershipScenarios:
    def test_join_mid_history(self) -> None:
        prices = make_panel()
        membership = pd.DataFrame(True, index=prices.index, columns=prices.columns)
        join_date = prices.index[len(prices) // 2]
        membership.loc[membership.index < join_date, "T03"] = False
        world = world_with_membership(membership, prices)
        assert_pit_invariant(world)
        # T03 must not be selected before joining.
        results = run_benchmark_zoo(
            world.market_data.close, membership=world.membership
        )
        weights = month_ends(results["cross_sectional_momentum"].weights)
        pre = weights.loc[weights.index < join_date, "T03"]
        assert (pre == 0.0).all()

    def test_exit_mid_history(self) -> None:
        prices = make_panel()
        membership = pd.DataFrame(True, index=prices.index, columns=prices.columns)
        exit_date = prices.index[len(prices) // 2]
        membership.loc[membership.index >= exit_date, "T04"] = False
        world = world_with_membership(membership, prices)
        assert_pit_invariant(world)
        results = run_benchmark_zoo(
            world.market_data.close, membership=world.membership
        )
        weights = month_ends(results["cross_sectional_momentum"].weights)
        post = weights.loc[weights.index >= exit_date, "T04"]
        assert (post == 0.0).all()

    def test_delisting(self) -> None:
        # Delisting == exit with the price series continuing: membership
        # must gate selection; the audit must not flag the mid-month hold.
        prices = make_panel()
        membership = pd.DataFrame(True, index=prices.index, columns=prices.columns)
        delist_date = prices.index[int(len(prices) * 0.6)]
        membership.loc[membership.index >= delist_date, "T05"] = False
        world = world_with_membership(membership, prices)
        assert_pit_invariant(world)

    def test_symbol_change(self) -> None:
        # T01 trades as T01A until the change date, then as T01B.
        prices = make_panel()
        change_date = prices.index[int(len(prices) * 0.5)]
        prices["T01B"] = prices["T01"]
        membership = pd.DataFrame(True, index=prices.index, columns=prices.columns)
        membership.loc[membership.index >= change_date, "T01"] = False
        membership.loc[membership.index < change_date, "T01B"] = False
        world = world_with_membership(membership, prices)
        assert_pit_invariant(world)
        results = run_benchmark_zoo(
            world.market_data.close, membership=world.membership
        )
        weights = month_ends(results["cross_sectional_momentum"].weights)
        # Old symbol never selected after the change; new symbol never
        # selected before it.
        old_late = weights.loc[weights.index >= change_date, "T01"]
        new_early = weights.loc[weights.index < change_date, "T01B"]
        assert (old_late == 0.0).all()
        assert (new_early == 0.0).all()

    def test_ipo_after_start(self) -> None:
        # A symbol listed mid-panel: the price panel may carry backfilled
        # history, but the PIT membership starts exactly at the IPO date.
        prices = make_panel()
        ipo_date = prices.index[int(len(prices) * 0.4)]
        membership = pd.DataFrame(True, index=prices.index, columns=prices.columns)
        membership.loc[membership.index < ipo_date, "T06"] = False
        world = world_with_membership(membership, prices)
        assert_pit_invariant(world)
        results = run_benchmark_zoo(
            world.market_data.close, membership=world.membership
        )
        weights = month_ends(results["cross_sectional_momentum"].weights)
        pre = weights.loc[weights.index < ipo_date, "T06"]
        assert (pre == 0.0).all()

    def test_nan_price_panel_rejected_clearly(self) -> None:
        # A price panel with missing observations is a data-integrity
        # problem: the engine must refuse it with a clear error rather
        # than silently fill or drop.
        prices = make_panel()
        prices.loc[prices.index[100], "T01"] = np.nan
        membership = pd.DataFrame(True, index=prices.index, columns=prices.columns)
        world = world_with_membership(membership, prices)
        with pytest.raises(ResearchInputError):
            run_benchmark_zoo(world.market_data.close, membership=world.membership)

    def test_absent_from_dataset(self) -> None:
        # Membership references a symbol with no price column at all: the
        # machinery must simply never select it (no crash, no phantom).
        prices = make_panel()
        membership = pd.DataFrame(True, index=prices.index, columns=prices.columns)
        membership["GHOST"] = True  # in the universe, not in the data
        world = world_with_membership(membership, prices)
        results = run_benchmark_zoo(
            world.market_data.close, membership=world.membership
        )
        weights = results["cross_sectional_momentum"].weights
        assert "GHOST" not in weights.columns
        assert_pit_invariant(world)

    def test_corporate_action_overlap(self) -> None:
        # A split at date X must not disturb membership gating: the
        # adjusted panel is price-level; membership is eligibility-level.
        prices = make_panel()
        split_date = prices.index[int(len(prices) * 0.5)]
        prices.loc[prices.index < split_date, "T02"] *= 2.0  # 2:1 split
        membership = pd.DataFrame(True, index=prices.index, columns=prices.columns)
        world = world_with_membership(membership, prices)
        assert_pit_invariant(world)
        # The split changes prices but not membership: T02 stays eligible
        # throughout and the zoo still runs deterministically.
        fundamentals = pd.DataFrame(
            {
                "date": pd.date_range(prices.index[0], prices.index[-1], freq="QE")
                .repeat(len(prices.columns))
                .tolist(),
                "symbol": list(prices.columns)
                * int(len(pd.date_range(prices.index[0], prices.index[-1], freq="QE"))),
                "roe": [0.1]
                * len(prices.columns)
                * int(len(pd.date_range(prices.index[0], prices.index[-1], freq="QE"))),
                "debt_to_equity": [0.4]
                * len(prices.columns)
                * int(len(pd.date_range(prices.index[0], prices.index[-1], freq="QE"))),
            }
        )
        results = run_benchmark_zoo(
            world.market_data.close,
            fundamentals=fundamentals,
            membership=world.membership,
        )
        assert set(results) == {entry["family_id"] for entry in ZOO_FAMILIES}

    def test_membership_index_shorter_than_data(self) -> None:
        # A strategy fed membership whose index doesn't cover the data is
        # aligned defensively (absent -> False), never error-prone.
        prices = make_panel()
        short_index = prices.index[: len(prices) // 2]
        membership = pd.DataFrame(True, index=short_index, columns=prices.columns)
        world = world_with_membership(membership, prices)
        results = run_benchmark_zoo(
            world.market_data.close, membership=world.membership
        )
        # No selection may EVER reference a date whose membership is
        # unknown: after the mask ends, all weights must be zero at every
        # selection date (conservative by construction).
        weights = month_ends(results["cross_sectional_momentum"].weights)
        late = weights.loc[weights.index > short_index[-1]]
        assert (late == 0.0).all().all()
        # ...and no selection before the mask end may include non-members.
        early = weights.loc[weights.index < short_index[-1]]
        audit = audit_rank_mask_order(early, membership, selection_dates=early.index)
        assert audit["violations"] == 0


class TestNaNBoolMaskRegression:
    """Regression: NaN.astype(bool) is True — missing membership must
    never become eligible. (Found by the PIT red-team.)"""

    def test_missing_dates_never_eligible(self) -> None:
        prices = make_panel(n_symbols=4, n_days=60)
        # Membership covers only 10 days; everything else is missing.
        short = prices.index[:10]
        membership = pd.DataFrame(True, index=short, columns=prices.columns)
        registry = __import__(
            "research.registry", fromlist=["StrategyRegistry"]
        ).StrategyRegistry()
        strategy = registry.build("cross_sectional_momentum", active_members=membership)
        signals = strategy.generate_signals(
            __import__("research.contracts", fromlist=["MarketData"]).MarketData(
                close=prices
            )
        )
        late = signals.values.loc[prices.index > short[-1]]
        assert (late == 0.0).all().all()

    def test_missing_symbol_never_eligible(self) -> None:
        prices = make_panel(n_symbols=4, n_days=60)
        membership = pd.DataFrame(
            True, index=prices.index, columns=prices.columns[:2]
        )  # T02, T03 absent from the mask
        from research.contracts import MarketData
        from research.registry import StrategyRegistry

        strategy = StrategyRegistry().build(
            "cross_sectional_momentum", active_members=membership
        )
        signals = strategy.generate_signals(MarketData(close=prices))
        missing = signals.values[["T02", "T03"]]
        assert (missing == 0.0).all().all()

    def test_momentum_quality_missing_membership_conservative(self) -> None:
        prices = make_panel(n_symbols=4, n_days=120)
        quarters = pd.date_range(prices.index[0], prices.index[-1], freq="QE")
        fundamentals = pd.DataFrame(
            {
                "date": [date for date in quarters for _ in prices.columns],
                "symbol": list(prices.columns) * len(quarters),
                "roe": [0.1, 0.12, 0.08, 0.15] * len(quarters),
                "debt_to_equity": [0.3, 0.4, 0.2, 0.5] * len(quarters),
            }
        )
        short = prices.index[:30]
        membership = pd.DataFrame(True, index=short, columns=prices.columns)
        from research.strategies import MomentumQualityStrategy

        strategy = MomentumQualityStrategy(
            fundamentals=fundamentals, active_members=membership
        )
        signals = strategy.generate_signals(
            __import__("research.contracts", fromlist=["MarketData"]).MarketData(
                close=prices
            )
        )
        late = signals.values.loc[prices.index > short[-1]]
        assert (late == 0.0).all().all()
