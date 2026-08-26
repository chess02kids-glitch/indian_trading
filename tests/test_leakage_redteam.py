"""Leakage red-team: every audit must catch its target and spare clean code."""

from __future__ import annotations

import numpy as np
import pandas as pd

from research.contracts import MarketData
from research.factors import MomentumFactor
from research.leakage import (
    LeakageAudit,
    audit_future_availability,
    audit_holdout_isolation,
    audit_lookahead,
    audit_rank_mask_order,
    audit_survivorship,
)
from research.registry import StrategyRegistry
from research.synthetic_worlds import build_world, leak_feature_for
from research.zoo import run_zoo_family


def make_close(n_symbols: int = 8, n_days: int = 400, seed: int = 3) -> pd.DataFrame:
    generator = np.random.default_rng(seed)
    index = pd.bdate_range("2024-01-02", periods=n_days)
    columns = [f"X{i}" for i in range(n_symbols)]
    returns = generator.normal(0.0004, 0.015, size=(n_days, n_symbols))
    return pd.DataFrame(
        100.0 * np.exp(np.cumsum(returns, axis=0)), index=index, columns=columns
    )


class TestLookaheadAudit:
    def test_clean_factor_passes(self) -> None:
        close = make_close()
        result = audit_lookahead(
            lambda panel: MomentumFactor(63).compute(MarketData(close=panel)),
            close,
        )
        assert result["clean"] is True
        assert result["violations"] == []

    def test_leaky_factor_flagged(self) -> None:
        world = build_world("E", seed=42, n_symbols=8, n_days=400)
        close = world.market_data.close

        # The world declares tomorrow's return as the leak; a leaky factor
        # computes it FROM THE PANEL (shift(-1)) — exactly what a
        # panel-derived look-ahead implementation looks like. The
        # truncation-recompute audit must flag it.
        def leaky_compute(panel: pd.DataFrame) -> pd.DataFrame:
            return (panel.shift(-1).div(panel) - 1.0).fillna(0.0).cumsum()

        result = audit_lookahead(leaky_compute, close)
        assert result["clean"] is False
        assert result["violations"]

    def test_external_data_smuggling_out_of_scope(self) -> None:
        # A computation that closes over an EXTERNAL future-information
        # frame (the world's leak feature) is not detectable by
        # recomputation — the truncation audit only sees panel-derived
        # look-ahead. The architectural defence is the feature contract:
        # factors must come from the registered registry computed on
        # MarketData, so external frames cannot enter the pipeline.
        world = build_world("E", seed=42, n_symbols=8, n_days=400)
        close = world.market_data.close
        leak = leak_feature_for(world)

        def smuggled(panel: pd.DataFrame) -> pd.DataFrame:
            return leak.reindex(index=panel.index).cumsum()

        result = audit_lookahead(smuggled, close)
        assert result["clean"] is True  # limitation documented, not hidden

    def test_future_shifted_clean_factor_flags_nothing(self) -> None:
        # A factor computed from yesterday's close is conservative, not
        # leaky: truncated recomputation must agree.
        close = make_close()
        result = audit_lookahead(
            lambda panel: panel.shift(1).pct_change().cumsum().fillna(0.0),
            close,
        )
        assert result["clean"] is True


class TestFutureAvailabilityAudit:
    def test_future_rows_detected(self) -> None:
        fundamentals = pd.DataFrame(
            {
                "date": ["2026-06-30", "2026-09-30", "2026-12-31"],
                "symbol": ["A", "A", "A"],
                "roe": [0.1, 0.1, 0.1],
            }
        )
        result = audit_future_availability(fundamentals, as_of="2026-08-26")
        assert result["clean"] is False
        assert result["future_rows"] == 2
        assert "2026-09-30" in result["future_dates"]

    def test_no_future_rows(self) -> None:
        fundamentals = pd.DataFrame(
            {
                "date": ["2026-03-31", "2026-06-30"],
                "symbol": ["A", "A"],
                "roe": [0.1, 0.1],
            }
        )
        result = audit_future_availability(fundamentals, as_of="2026-08-26")
        assert result["clean"] is True


def _month_end_dates(index: pd.DatetimeIndex) -> list[pd.Timestamp]:
    """Last business day of each month (the engine's selection dates)."""
    return list(index[~index.to_period("M").duplicated(keep="last")])


class TestRankMaskOrderAudit:
    def test_correct_strategy_never_selects_non_members(self) -> None:
        world = build_world("F", seed=20260824, n_symbols=16, n_days=400)
        result = run_zoo_family(
            "cross_sectional_momentum",
            world.market_data.close,
            membership=world.membership,
        )
        audit = audit_rank_mask_order(
            result.weights,
            world.membership,
            selection_dates=_month_end_dates(result.weights.index),
        )
        assert audit["clean"] is True
        # Between rebalances the engine legitimately holds pre-delist
        # positions, so the strict daily check reports them — documented
        # simulation behavior, not a selection bug.
        strict = audit_rank_mask_order(result.weights, world.membership)
        assert strict["violations"] >= audit["violations"]

    def test_rank_then_mask_bug_detected(self) -> None:
        # Deliberately reproduce the classic bug: rank every symbol, then
        # mask. The final panel contains only members, so the missing-mask
        # check passes — but the selections differ from the mask-before-
        # rank reference (non-members stole ranks), which the order check
        # must flag.
        world = build_world("F", seed=20260824, n_symbols=16, n_days=400)
        close = world.market_data.close
        momentum = MomentumFactor(63).compute(MarketData(close=close))
        rank_all = momentum.rank(axis=1, pct=True, method="first")
        buggy = rank_all.ge(0.75).astype(float)
        # mask after ranking (the bug):
        buggy = buggy.where(world.membership, 0.0)
        # correct mask-before-rank reference:
        masked = momentum.where(world.membership)
        rank_members = masked.rank(axis=1, pct=True, method="first")
        reference = rank_members.ge(0.75).where(rank_members.notna()).fillna(0.0)
        audit = audit_rank_mask_order(
            buggy,
            world.membership,
            reference=reference,
            selection_dates=_month_end_dates(close.index),
        )
        assert audit["violations"] == 0  # no non-member selected (mask applied)
        assert audit["clean"] is False  # ...but the ORDER is wrong
        assert audit["order_violations"]

    def test_quantile_strategies_respect_membership(self) -> None:
        world = build_world("F", seed=20260824, n_symbols=16, n_days=400)
        registry = StrategyRegistry()
        for registry_id in (
            "cross_sectional_momentum",
            "low_volatility",
            "reversal",
        ):
            strategy = registry.build(registry_id, active_members=world.membership)
            signals = strategy.generate_signals(world.market_data)
            audit = audit_rank_mask_order(
                signals.values,
                world.membership,
                selection_dates=_month_end_dates(signals.values.index),
            )
            assert audit["clean"] is True, f"{registry_id}: {audit['violations']}"
        # quality additionally needs the fundamentals frame
        strategy = registry.build(
            "quality", active_members=world.membership, fundamentals=world.fundamentals
        )
        signals = strategy.generate_signals(world.market_data)
        audit = audit_rank_mask_order(
            signals.values,
            world.membership,
            selection_dates=_month_end_dates(signals.values.index),
        )
        assert audit["clean"] is True


class TestSurvivorshipAudit:
    def test_flags_never_eligible_symbol(self) -> None:
        prices = make_close(n_symbols=5, n_days=120)
        membership = pd.DataFrame(True, index=prices.index, columns=prices.columns)
        membership["X4"] = False  # priced but never eligible
        result = audit_survivorship(membership, prices)
        assert result["clean"] is False
        assert "X4" in result["priced_but_never_eligible"]

    def test_world_f_membership_is_clean(self) -> None:
        world = build_world("F", seed=20260824, n_symbols=16, n_days=400)
        result = audit_survivorship(world.membership, world.market_data.close)
        # Priced-but-never-eligible is a data issue; world F has none.
        assert result["priced_but_never_eligible"] == []
        # Delisted-but-still-priced is expected (raw panel + PIT mask).
        assert result["delisted_but_still_priced"]


class TestHoldoutIsolationAudit:
    def test_disjoint_windows_pass(self) -> None:
        result = audit_holdout_isolation(
            ("2024-01-01", "2025-06-30"), ("2025-07-01", "2026-06-30")
        )
        assert result["clean"] is True
        assert result["gap_days"] == 1

    def test_overlapping_windows_fail(self) -> None:
        result = audit_holdout_isolation(
            ("2024-01-01", "2025-06-30"), ("2025-06-01", "2026-06-30")
        )
        assert result["clean"] is False
        assert result["overlapping"] is True


class TestLeakageAuditAggregate:
    def test_aggregate_verdict(self) -> None:
        clean = {
            "a": audit_future_availability(
                pd.DataFrame({"date": ["2026-06-30"], "symbol": ["A"], "roe": [0.1]}),
                as_of="2026-08-26",
            ),
            "b": audit_holdout_isolation(
                ("2024-01-01", "2025-06-30"), ("2025-07-01", "2026-06-30")
            ),
        }
        audit = LeakageAudit(clean)
        assert audit.clean() is True
        assert audit.findings() == {}

        dirty = dict(clean)
        dirty["c"] = audit_future_availability(
            pd.DataFrame({"date": ["2026-12-31"], "symbol": ["A"], "roe": [0.1]}),
            as_of="2026-08-26",
        )
        combined = LeakageAudit(dirty)
        assert combined.clean() is False
        assert set(combined.findings()) == {"c"}
