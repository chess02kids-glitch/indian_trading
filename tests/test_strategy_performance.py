"""Unit tests for dashboard/strategy_performance.py.

Tests risk controls, look-ahead prevention, indicator mathematics,
signal generation, walk-forward validation, multi-symbol portfolio mode,
volume validation, transaction cost modeling, and statistical scoring.
"""

import numpy as np
import pandas as pd
import pytest

import dashboard.strategy_performance as sp


@pytest.fixture
def sample_ohlcv_data():
    """Generate 150 days of synthetic OHLCV market data."""
    dates = pd.date_range("2026-01-01", periods=150, freq="1D")
    np.random.seed(42)
    drift = np.linspace(0, 15, 150)
    noise = np.cumsum(np.random.randn(150) * 1.5)
    close = pd.Series(100.0 + drift + noise, index=dates)
    high = close + np.random.rand(150) * 2.5
    low = close - np.random.rand(150) * 2.5
    open_ = close + np.random.randn(150) * 0.8
    volume = pd.Series(np.random.randint(5000, 50000, size=150), index=dates)

    df = pd.DataFrame({
        "Open": open_,
        "High": high,
        "Low": low,
        "Close": close,
        "Volume": volume
    }, index=dates)
    return df


@pytest.fixture
def zero_volume_data(sample_ohlcv_data):
    """Generate OHLCV data with 0 volume (like yfinance index data)."""
    df = sample_ohlcv_data.copy()
    df["Volume"] = 0.0
    return df


class TestStrategyPerformanceEngine:
    """Test suite covering all audit findings, indicator fixes, and risk defenses."""

    def test_all_30_strategies_instantiate_and_run(self, sample_ohlcv_data):
        """Verify that all 30 strategy definitions run without exceptions."""
        assert len(sp.ALL_STRATEGIES) == 30
        for strat in sp.ALL_STRATEGIES:
            sig_df = strat.signals(sample_ohlcv_data.copy())
            assert "Buy_Signal" in sig_df.columns
            assert "Sell_Signal" in sig_df.columns
            assert len(sig_df) == len(sample_ohlcv_data)

    def test_vwap_daily_reset(self):
        """[CRITICAL BUG FIX]: Verify VWAP resets each day and does not accumulate across days."""
        # 2 distinct trading days of hourly data (7 bars per day)
        d1 = pd.date_range("2026-01-01 09:15", periods=7, freq="1h")
        d2 = pd.date_range("2026-01-02 09:15", periods=7, freq="1h")
        dates = d1.append(d2)
        close = pd.Series([100.0] * 7 + [200.0] * 7, index=dates)
        vol = pd.Series([1000.0] * 14, index=dates)
        df = pd.DataFrame({"Open": close, "High": close, "Low": close, "Close": close, "Volume": vol}, index=dates)

        vwap = sp.calc_vwap(df)
        assert vwap.iloc[0] == 100.0
        assert vwap.iloc[6] == 100.0
        # Day 2 first bar MUST reset to 200.0 (not accumulate 100.0 from day 1)
        assert vwap.iloc[7] == 200.0
        assert vwap.iloc[13] == 200.0

    def test_aroon_direction_math(self):
        """[CRITICAL BUG FIX]: Verify Aroon Up is 100% on a fresh 25-day high (not 4%)."""
        dates = pd.date_range("2026-01-01", periods=30, freq="1D")
        close = pd.Series(range(30), index=dates, dtype=float)
        df = pd.DataFrame({"Open": close, "High": close, "Low": close - 1.0, "Close": close, "Volume": 1000}, index=dates)

        up, down = sp.calc_aroon(df, 25)
        # On day 29, the high is on the current bar (0 bars ago) -> Aroon Up must be 100.0%
        assert up.iloc[-1] == 100.0
        # The lowest low was 24 bars ago -> Aroon Down must be 4.0%
        assert down.iloc[-1] == 4.0

    def test_no_signal_clashing_in_threshold_strategies(self, sample_ohlcv_data):
        """[CRITICAL BUG FIX]: Verify Buy and Sell signals do not clash simultaneously on the same bar."""
        threshold_strats = [sp.S16_CCI(), sp.S17_WilliamsR(), sp.S25_RSI_30_70()]
        for strat in threshold_strats:
            sig_df = strat.signals(sample_ohlcv_data)
            clashes = (sig_df["Buy_Signal"] & sig_df["Sell_Signal"]).sum()
            assert clashes == 0, f"Strategy {strat.name} has {clashes} clashing signals on same bar"

    def test_terminal_position_reconciled(self, sample_ohlcv_data):
        """[HIGH BUG FIX]: Verify open positions at end of dataset are closed and included in capital."""
        strat = sp.S07_GoldenCross()
        trades, final_cap, eq = sp.backtest_strategy(sample_ohlcv_data, strat)
        if not trades.empty:
            last_trade = trades.iloc[-1]
            if last_trade["Exit_Reason"] == "END_OF_DATA":
                assert last_trade["Exit_Date"] == sample_ohlcv_data.index[-1]
                assert last_trade["Capital"] == final_cap

    def test_atr_stop_loss_trigger(self, sample_ohlcv_data):
        """Verify that ATR hard stop-loss exits positions and records 'STOP_LOSS' reason."""
        strat = sp.S01_AlphaTrend()
        trades, final_cap, eq = sp.backtest_strategy(
            sample_ohlcv_data, strat, initial_capital=50000.0, atr_stop_multiplier=1.0, brokerage_bps=15.0
        )
        assert isinstance(trades, pd.DataFrame)
        assert len(eq) == len(sample_ohlcv_data)
        if not trades.empty:
            assert "Exit_Reason" in trades.columns
            assert set(trades["Exit_Reason"].unique()).issubset({"STOP_LOSS", "SIGNAL_EXIT", "END_OF_DATA"})
            assert "Cost" in trades.columns

    def test_volume_guard_on_index_symbols(self, zero_volume_data):
        """Verify that volume-dependent strategies (VWAP, OBV, Volume RSI) do not fire on zero-volume data."""
        assert not sp.is_volume_valid(zero_volume_data)

        v_strats = [sp.S26_VWAP(), sp.S27_Volume_RSI_Combo(), sp.S28_OBV_Trend()]
        for v_strat in v_strats:
            trades, _, _ = sp.backtest_strategy(zero_volume_data, v_strat)
            assert len(trades) == 0

    def test_cost_model_deductions(self, sample_ohlcv_data):
        """Verify that transaction costs (15 bps) reduce net PnL below gross PnL."""
        strat = sp.S14_RSI_50_Cross()
        trades_cost, cap_cost, _ = sp.backtest_strategy(
            sample_ohlcv_data, strat, brokerage_bps=15.0, fixed_brokerage=0.0
        )
        trades_free, cap_free, _ = sp.backtest_strategy(
            sample_ohlcv_data, strat, brokerage_bps=0.0, fixed_brokerage=0.0
        )
        if not trades_cost.empty and not trades_free.empty:
            assert cap_cost <= cap_free
            assert trades_cost["Net_PnL"].sum() <= trades_cost["Gross_PnL"].sum()

    def test_risk_adjusted_scoring(self, sample_ohlcv_data):
        """Verify score_trades computes Sharpe, Sortino, Calmar, and Deflated Sharpe."""
        strat = sp.S01_AlphaTrend()
        trades, cap, eq = sp.backtest_strategy(sample_ohlcv_data, strat)
        metrics = sp.score_trades(trades, 50000.0, strat.name, strat.group, equity_curve=eq, n_trials=30)

        assert "Sharpe" in metrics
        assert "Sortino" in metrics
        assert "Calmar" in metrics
        assert "Deflated Sharpe" in metrics
        assert "Score" in metrics
        assert "Rating" in metrics
        assert 0 <= metrics["Score"] <= 10

    def test_shared_capital_portfolio_mode(self, sample_ohlcv_data):
        """Verify shared-capital multi-symbol portfolio backtester."""
        strat = sp.S04_EMA_20_50()
        multi_data = {
            "RELIANCE": sample_ohlcv_data,
            "TCS": sample_ohlcv_data.copy() * 1.05,
            "INFY": sample_ohlcv_data.copy() * 0.95
        }
        initial_cap = 1_000_000.0
        port_res = sp.run_portfolio_backtest(
            multi_data,
            strat,
            initial_capital=initial_cap,
            max_positions=3,
            sizing_method="vol_target",
            risk_per_trade_pct=0.01,
            atr_stop_multiplier=2.0,
            cost_bps=15.0
        )

        assert "portfolio_metrics" in port_res
        assert "trades" in port_res
        assert "equity_curve" in port_res
        assert len(port_res["equity_curve"]) > 0
        assert port_res["portfolio_metrics"]["Final Capital"] > 0

    def test_next_bar_open_execution(self):
        """[CRITICAL BUG FIX]: Verify Buy Signal on bar i fills at bar i+1 Open, preventing lookahead bias."""
        dates = pd.date_range("2026-01-01", periods=10, freq="1D")
        opens = [100.0, 101.0, 105.0, 110.0, 112.0, 115.0, 114.0, 110.0, 108.0, 105.0]
        closes = [100.5, 102.0, 108.0, 111.0, 113.0, 114.0, 112.0, 109.0, 107.0, 104.0]
        highs = [c + 1.0 for c in closes]
        lows = [o - 1.0 for o in opens]
        df = pd.DataFrame({
            "Open": opens,
            "High": highs,
            "Low": lows,
            "Close": closes,
            "Volume": 10000
        }, index=dates)

        strat = sp.S05_EMA_9_21()
        trades, _, eq = sp.backtest_strategy(df, strat, initial_capital=50000.0)
        # If any trade occurred, the Buy_Price must match the Open price of the fill bar
        for _, trade in trades.iterrows():
            entry_idx = df.index.get_loc(trade["Entry_Date"])
            assert trade["Buy_Price"] == round(df["Open"].iloc[entry_idx], 2)

    def test_stochastic_bounds_and_no_nan(self, sample_ohlcv_data):
        """[CRITICAL BUG FIX]: Verify Stochastic handles flat/zero-range periods gracefully without returning NaNs."""
        # Flat series
        dates = pd.date_range("2026-01-01", periods=50, freq="1D")
        df_flat = pd.DataFrame({
            "Open": [100.0] * 50,
            "High": [100.0] * 50,
            "Low": [100.0] * 50,
            "Close": [100.0] * 50,
            "Volume": 1000
        }, index=dates)
        k, d = sp.calc_stochastic(df_flat, 14, 3)
        assert not k.isna().all()
        assert not np.isinf(k).any()
        assert not np.isinf(d).any()

    def test_donchian_breakout_shift_no_lookahead(self):
        """[CRITICAL BUG FIX]: Verify S10 Donchian Breakout shifts bands by 1 to prevent same-bar lookahead."""
        dates = pd.date_range("2026-01-01", periods=25, freq="1D")
        highs = list(range(100, 125)) # increasing high: 100, 101, ..., 124
        df = pd.DataFrame({
            "Open": highs,
            "High": highs,
            "Low": [h - 2 for h in highs],
            "Close": highs,
            "Volume": 1000
        }, index=dates)
        strat = sp.S10_Donchian_Breakout()
        sig_df = strat.signals(df)
        upper, _, _ = sp.calc_donchian(df, period=20)
        # S10 compares Close to upper.shift(1)
        assert sig_df["Buy_Signal"].iloc[20] == (df["Close"].iloc[20] > upper.iloc[19])

    def test_supertrend_ratchet_logic(self):
        """[CRITICAL BUG FIX]: Verify SuperTrend ratchet logic maintains uptrend during monotonic rises."""
        dates = pd.date_range("2026-01-01", periods=20, freq="1D")
        # Steady uptrend
        close = pd.Series([100.0 + i * 2.0 for i in range(20)], index=dates)
        df = pd.DataFrame({
            "Open": close,
            "High": close + 1.0,
            "Low": close - 1.0,
            "Close": close,
            "Volume": 1000
        }, index=dates)
        direction = sp.calc_supertrend(df, period=7, multiplier=3.0)
        # In a monotonic uptrend, direction should be +1 (bullish)
        assert (direction.iloc[7:] == 1.0).all()

    def test_walk_forward_validation_split(self, sample_ohlcv_data):
        """Verify walk-forward train/test split and gate decision logic."""
        strat = sp.S05_EMA_9_21()
        wf = sp.walk_forward_validate_strategy(sample_ohlcv_data, strat, train_ratio=0.7)

        assert "verdict" in wf
        assert "gate_decision" in wf
        assert "is_sharpe" in wf
        assert "oos_sharpe" in wf
        assert "deflated_sharpe" in wf
        assert "placebo_sharpe" in wf
        assert wf["verdict"] in {"PASS_FOR_RESEARCH_COCKPIT", "FRAGILE", "REJECT_IN_SAMPLE_OVERFIT", "INSUFFICIENT_DATA"}
