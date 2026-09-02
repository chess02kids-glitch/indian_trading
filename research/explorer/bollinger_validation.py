"""Deep, adversarial validation for the Bollinger mean-reversion candidate.

The discovery sweep suggested that monthly selection of names below a
Bollinger lower band (cross-sectional short-term reversion) is profitable on
the 2023-2026 NSE panel. This module checks whether that result survives:

* realistic India one-way transaction costs (0 / 12 / 25 / 40 bps);
* a market-neutral version (long bottom-z, short top-z) to separate the
  cross-sectional reversal edge from market beta;
* rebalance frequency (weekly / monthly / quarterly);
* out-of-sample split where parameters are chosen only on the train window;
* three complete calendar years (2023-2025), excluding the partial 2026;
* risk tail (worst month, VaR95, expected shortfall, max drawdown);
* exposure concentration (top contributing names);
* universe robustness (PIT mask on vs off).

Output: ``data/features/bollinger_validation.json`` and a summary table.
"""

from __future__ import annotations

import itertools
from pathlib import Path

import numpy as np
import pandas as pd

from .io import (
    load_eod_panels,
    load_pit_universe,
    panel_universe_mask,
    resolve_research_universe,
)
from .sim import DiscoveryConfig, simulate
from .strategies import _bollinger_weights

ROOT = Path(__file__).resolve().parents[2]
FEATURES_DIR = ROOT / "data" / "features"


def _rank_pct(frame: pd.DataFrame) -> pd.DataFrame:
    return frame.rank(axis=1, pct=True, method="first")


def long_only_weights(
    close: pd.DataFrame, window: int, std: float, top_share: float
) -> pd.DataFrame:
    return _bollinger_weights(close, window, std, top_share)


def long_short_weights(
    close: pd.DataFrame, window: int, std: float, top_share: float
) -> pd.DataFrame:
    """Long the bottom ``top_share`` of the z-score cross-section, short the top.

    This is the *relative* cross-sectional benchmark-neutral construction. It
    always has positions and isolates the mean-reversion spread from the
    long-only beta of the hard-gate version. ``std`` is retained as a parameter
    for signature compatibility but is not used as a hard gate here.

    Simulated with a 1.0 gross, net-zero portfolio. Note: shorting requires
    borrow availability, which this local dataset does NOT contain, so this is
    a research isolation test, not a directly tradable rule.
    """
    sma = close.rolling(window).mean()
    sd = close.rolling(window).std()
    z = (close - sma) / sd.replace(0.0, np.nan)
    r = _rank_pct(z)
    long_mask = r.ge(1.0 - top_share)
    short_mask = r.le(top_share)
    long_w = long_mask.astype(float).div(long_mask.sum(axis=1).replace(0, np.nan), axis=0).fillna(0.0)
    short_w = short_mask.astype(float).div(short_mask.sum(axis=1).replace(0, np.nan), axis=0).fillna(0.0)
    net = long_w - short_w
    gross = net.abs().sum(axis=1).replace(0, np.nan)
    return net.div(gross, axis=0).fillna(0.0)


def _norm_long(row: pd.Series) -> pd.Series:
    s = row.sum()
    if s > 0 and (row >= 0).all():
        return row / s
    return row


def _daily_metrics(returns: pd.Series) -> dict[str, float]:
    returns = returns.dropna()
    if returns.empty:
        return {}
    equity = (1.0 + returns).cumprod()
    total = float(equity.iloc[-1] - 1.0)
    n = len(returns)
    ann = float((1.0 + total) ** (252.0 / n) - 1.0)
    vol = float(returns.std(ddof=1) * np.sqrt(252))
    sharpe = float(ann / vol) if vol > 0 else 0.0
    dl = returns[returns < 0]
    dvol = float(dl.std(ddof=1) * np.sqrt(252)) if len(dl) > 1 else 0.0
    sortino = float(ann / dvol) if dvol > 0 else 0.0
    dd = equity / equity.cummax() - 1.0
    mdd = float(dd.min())
    monthly = returns.resample("ME").apply(lambda x: (1.0 + x).prod() - 1.0)
    worst_month = float(monthly.min()) if len(monthly) else 0.0
    daily = returns
    var95 = float(np.percentile(daily, 5))
    es95 = float(daily[daily <= var95].mean()) if (daily <= var95).any() else float(var95)
    return {
        "total_return": total,
        "cagr": ann,
        "volatility": vol,
        "sharpe": sharpe,
        "sortino": sortino,
        "max_drawdown": mdd,
        "worst_month": worst_month,
        "var95_daily": var95,
        "es95_daily": es95,
        "periods": float(n),
    }


def _turnover(result, cost_rate: float) -> dict[str, float]:
    turn = result.turnover
    rp = turn[turn > 0]
    return {
        "rebalance_count": int(len(rp)),
        "avg_turnover": float(rp.mean()) if len(rp) else 0.0,
        "total_turnover": float(turn.sum()),
        "total_cost": float((turn * cost_rate).sum()),
        "cost_drag_pct_of_gross": (
            float((turn * cost_rate).sum() / abs((1.0 + result.returns).prod() - 1.0) * 100)
            if abs((1.0 + result.returns).prod() - 1.0) > 1e-12
            else 0.0
        ),
    }


def _beta_alpha(strategy_returns: pd.Series, market_returns: pd.Series) -> dict[str, float]:
    common = pd.concat([strategy_returns.rename("strat"), market_returns.rename("mkt")], axis=1).dropna()
    if len(common) < 2:
        return {"beta": float("nan"), "annualized_alpha": float("nan"), "correlation": float("nan")}
    cov = float(np.cov(common["strat"], common["mkt"])[0, 1])
    var = float(np.var(common["mkt"]))
    beta = cov / var if var > 0 else float("nan")
    alpha_daily = float((common["strat"] - beta * common["mkt"]).mean())
    corr = float(np.corrcoef(common["strat"], common["mkt"])[0, 1])
    return {
        "beta": beta,
        "annualized_alpha": alpha_daily * 252,
        "correlation": corr,
    }


def _top_contrib(weights: pd.DataFrame, prices: pd.DataFrame, top: int = 8) -> dict[str, float]:
    rets = prices.pct_change().fillna(0.0)
    contrib = (rets * weights.shift(1).fillna(0.0)).sum()
    sorted_c = contrib.sort_values()
    top_str = sorted_c.tail(top)
    worst = sorted_c.head(top)
    return {
        "best_five": top_str.to_dict(),
        "worst_five": worst.to_dict(),
        "sum_positive": float(sorted_c[sorted_c > 0].sum()),
        "sum_negative": float(sorted_c[sorted_c < 0].sum()),
    }


def run() -> pd.DataFrame:
    print("Loading panels ...")
    panels = load_eod_panels()
    close = panels["close"]
    universe = load_pit_universe()
    avail = resolve_research_universe(close, universe)
    close = close.loc[:, [c for c in close.columns if c in avail]]
    panels = {k: v.loc[:, close.columns].reindex(index=close.index) for k, v in panels.items()}
    mask = panel_universe_mask(universe, close.index, close.columns)

    eq_weights = pd.DataFrame(
        1.0 / len(close.columns), index=close.index, columns=close.columns
    )
    eq_res = simulate(close, eq_weights, config=DiscoveryConfig(rebalance_frequency="M", one_way_cost_bps=0))
    market_ret = eq_res.returns

    results = []

    # Cost sensitivity, monthly, long-only, PIT mask on (continuous window).
    for cost in (0.0, 12.0, 25.0, 40.0):
        w = _bollinger_weights(close, 30, 2.5, 0.25).where(mask, 0.0).apply(_norm_long, axis=1)
        res = simulate(close, w, config=DiscoveryConfig(rebalance_frequency="M", one_way_cost_bps=cost))
        m = _daily_metrics(res.returns)
        m.update(_turnover(res, cost / 10_000))
        m.update(_beta_alpha(res.returns, market_ret))
        m.update(_top_contrib(w, close))
        m.update(
            {
                "mode": "cost_sensitivity",
                "window": 30,
                "std": 2.5,
                "top_share": 0.25,
                "one_way_cost_bps": cost,
                "long_short": False,
                "mask": True,
                "rebalance": "M",
            }
        )
        results.append(m)

    # Market-neutral long-short (PIT mask on), monthly.
    for cost in (0.0, 12.0, 25.0, 40.0):
        w = long_short_weights(close, 30, 2.5, 0.25).where(mask, 0.0)
        res = simulate(close, w, config=DiscoveryConfig(rebalance_frequency="M", one_way_cost_bps=cost))
        m = _daily_metrics(res.returns)
        m.update(_turnover(res, cost / 10_000))
        m.update(_beta_alpha(res.returns, market_ret))
        m.update(_top_contrib(w, close))
        m.update(
            {
                "mode": "market_neutral",
                "window": 30,
                "std": 2.5,
                "top_share": 0.25,
                "one_way_cost_bps": cost,
                "long_short": True,
                "mask": True,
                "rebalance": "M",
            }
        )
        results.append(m)

    # Rebalance-frequency sensitivity (long-only, 12 bps, PIT mask on).
    for freq in ("W", "M", "Q"):
        w = _bollinger_weights(close, 30, 2.5, 0.25).where(mask, 0.0).apply(_norm_long, axis=1)
        res = simulate(close, w, config=DiscoveryConfig(rebalance_frequency=freq, one_way_cost_bps=12))
        m = _daily_metrics(res.returns)
        m.update(_turnover(res, 12 / 10_000))
        m.update(_beta_alpha(res.returns, market_ret))
        m.update(
            {
                "mode": "rebalance_frequency",
                "window": 30,
                "std": 2.5,
                "top_share": 0.25,
                "one_way_cost_bps": 12,
                "long_short": False,
                "mask": True,
                "rebalance": freq,
            }
        )
        results.append(m)

    # Universe-robustness sensitivity: mask on versus off, long-only, monthly, 12 bps.
    for use_mask in (True, False):
        w = _bollinger_weights(close, 30, 2.5, 0.25)
        if use_mask:
            w = w.where(mask, 0.0)
        w = w.apply(_norm_long, axis=1)
        res = simulate(close, w, config=DiscoveryConfig(rebalance_frequency="M", one_way_cost_bps=12))
        m = _daily_metrics(res.returns)
        m.update(_turnover(res, 12 / 10_000))
        m.update(_beta_alpha(res.returns, market_ret))
        m.update(
            {
                "mode": "universe_robustness",
                "window": 30,
                "std": 2.5,
                "top_share": 0.25,
                "one_way_cost_bps": 12,
                "long_short": False,
                "mask": bool(use_mask),
                "rebalance": "M",
            }
        )
        results.append(m)

    # Beta-hedged long-only: long hard-gate Bollinger, short beta * equal-weight.
    # This separates the directional/market beta from the strategy's own alpha.
    boll_res = simulate(
        close,
        _bollinger_weights(close, 30, 2.5, 0.25).where(mask, 0.0).apply(_norm_long, axis=1),
        config=DiscoveryConfig(rebalance_frequency="M", one_way_cost_bps=12),
    )
    beta = float(np.cov(pd.concat([boll_res.returns.rename("s"), market_ret.rename("m")], axis=1).dropna()["s"],
                        pd.concat([boll_res.returns.rename("s"), market_ret.rename("m")], axis=1).dropna()["m"])[0, 1] /
               np.var(market_ret.dropna()))
    hedged = boll_res.returns - beta * market_ret.reindex(boll_res.returns.index)
    hedged_m = _daily_metrics(hedged)
    hedged_m.update(
        {
            "mode": "beta_hedged_long_only",
            "window": 30,
            "std": 2.5,
            "one_way_cost_bps": 12,
            "long_short": True,
            "mask": True,
            "rebalance": "M",
            "beta_used": beta,
        }
    )
    results.append(hedged_m)

    # Cost sensitivity for the two lower-drawdown variants found in the grid.
    for window, std in ((15, 3.0), (20, 2.5)):
        for cost in (0.0, 12.0, 25.0, 40.0):
            w = _bollinger_weights(close, window, std, 0.25).where(mask, 0.0).apply(_norm_long, axis=1)
            res = simulate(close, w, config=DiscoveryConfig(rebalance_frequency="M", one_way_cost_bps=cost))
            m = _daily_metrics(res.returns)
            m.update(_turnover(res, cost / 10_000))
            m.update(_beta_alpha(res.returns, market_ret))
            m.update(
                {
                    "mode": "cost_sensitivity_alt",
                    "window": window,
                    "std": std,
                    "top_share": 0.25,
                    "one_way_cost_bps": cost,
                    "long_short": False,
                    "mask": True,
                    "rebalance": "M",
                }
            )
            results.append(m)

    # Full three-year (2023-2025) results and the 2026 partial-year warning.
    for label, start, end in [
        ("three_full_years", "2023-01-01", "2025-12-31"),
        ("partial_2026", "2026-01-01", "2026-08-31"),
    ]:
        sub_close = close.loc[start:end]
        sub_mask = mask.loc[start:end]
        w = _bollinger_weights(sub_close, 30, 2.5, 0.25).where(sub_mask, 0.0).apply(_norm_long, axis=1)
        res = simulate(sub_close, w, config=DiscoveryConfig(rebalance_frequency="M", one_way_cost_bps=12))
        m = _daily_metrics(res.returns)
        m.update(_turnover(res, 12 / 10_000))
        m.update(_beta_alpha(res.returns, market_ret.reindex(sub_close.index)))
        m.update(
            {
                "mode": "period_split",
                "window": 30,
                "std": 2.5,
                "top_share": 0.25,
                "one_way_cost_bps": 12,
                "long_short": False,
                "mask": True,
                "rebalance": "M",
                "period": label,
            }
        )
        results.append(m)

    # Out-of-sample: use the full parameter grid, choose the best *train* candidate
    # for each mode, then report its test metrics. This is the strict test.
    train_close = close.loc["2023-01-01":"2024-12-31"]
    test_close = close.loc["2025-01-01":"2026-08-31"]
    train_mask = mask.loc[train_close.index]
    test_mask = mask.loc[test_close.index]
    grid = list(itertools.product([15, 20, 30, 40], [1.5, 2.0, 2.5, 3.0]))
    grid_results = []
    for window, std in grid:
        w_train = _bollinger_weights(train_close, window, std, 0.25).where(train_mask, 0.0).apply(_norm_long, axis=1)
        res_train = simulate(train_close, w_train, config=DiscoveryConfig(rebalance_frequency="M", one_way_cost_bps=12))
        grid_results.append((window, std, res_train.metrics.get("sharpe", float("nan"))))
    best = max(grid_results, key=lambda x: x[2])
    w_test = _bollinger_weights(test_close, best[0], best[1], 0.25).where(test_mask, 0.0).apply(_norm_long, axis=1)
    res_test = simulate(test_close, w_test, config=DiscoveryConfig(rebalance_frequency="M", one_way_cost_bps=12))
    test_m = _daily_metrics(res_test.returns)
    test_m.update(_turnover(res_test, 12 / 10_000))
    test_m.update(_beta_alpha(res_test.returns, market_ret.reindex(test_close.index)))
    test_m.update(
        {
            "mode": "strict_out_of_sample",
            "chosen_window": best[0],
            "chosen_std": best[1],
            "train_sharpe": float(best[2]),
            "one_way_cost_bps": 12,
            "long_short": False,
            "mask": True,
            "rebalance": "M",
            "window": best[0],
            "std": best[1],
        }
    )
    results.append(test_m)

    frame = pd.DataFrame(results)
    frame.to_json(FEATURES_DIR / "bollinger_validation.json", orient="records", indent=2)
    return frame


def main() -> int:
    frame = run()
    pd.set_option("display.width", 250)
    pd.set_option("display.max_columns", None)
    print(frame[
        [
            "mode",
            "window",
            "std",
            "one_way_cost_bps",
            "rebalance",
            "mask",
            "cagr",
            "volatility",
            "sharpe",
            "max_drawdown",
            "worst_month",
            "avg_turnover",
            "total_cost",
            "beta",
            "annualized_alpha",
            "correlation",
        ]
    ].to_string(index=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
