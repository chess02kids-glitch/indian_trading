"""Risk and interaction detail for the Bollinger mean-reversion candidates.

Focus: are the ultra-low-drawdown variants (15/3.0, 20/2.5) real or the result
of being thinly traded, concentrated, or month-lucky? Produces JSON with:

* monthly P&L table and worst months;
* rolling 6-month Sharpe and worst rolling window;
* per-rebalance number of positions, average gross weight, worst single weight;
* liquidity proxy (share-volume) of selected names vs universe;
* returns split by start-date (2024-, 2025-) to avoid anchoring on 2023-01.
"""

from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pandas as pd

from .bollinger_validation import long_only_weights, _daily_metrics
from .io import load_eod_panels, load_pit_universe, panel_universe_mask, resolve_research_universe
from .sim import DiscoveryConfig, simulate

ROOT = Path(__file__).resolve().parents[2]
FEATURES_DIR = ROOT / "data" / "features"


def _norm_long(row: pd.Series) -> pd.Series:
    s = row.sum()
    if s > 0 and (row >= 0).all():
        return row / s
    return row


def _monthly(returns: pd.Series) -> pd.Series:
    return returns.resample("ME").apply(lambda x: (1.0 + x).prod() - 1.0)


def _rolling_sharpe(returns: pd.Series, window: int = 126) -> pd.Series:
    # Pass raw daily returns, not cumulative values, to the window function.
    return returns.rolling(window).apply(
        lambda x: ((1.0 + x).prod() ** (252.0 / len(x)) - 1.0) / (x.std() * np.sqrt(252))
        if len(x) and x.std() > 0
        else np.nan,
        raw=False,
    )


def _position_stats(weights: pd.DataFrame) -> dict[str, float]:
    active = weights.gt(1e-9)
    counts = active.sum(axis=1)
    counts = counts[counts > 0]
    return {
        "avg_positions": float(counts.mean()),
        "min_positions": float(counts.min()),
        "max_positions": float(counts.max()),
        "avg_top_weight": float(weights.max(axis=1).mean()),
        "max_single_weight": float(weights.max(axis=1).max()),
    }


def _liquidity_check(
    selected_weights: pd.DataFrame, volume: pd.DataFrame, close: pd.DataFrame
) -> dict[str, float]:
    # Approximate traded value per day = volume * close (₹, actually shares * price).
    traded_value = volume * close
    # Weighted-average daily traded value of the portfolio (₹).
    w = selected_weights.shift(1).fillna(0.0)
    portfolio_value = (traded_value * w).sum(axis=1)
    universe_value = traded_value.mean(axis=1)
    active = w.abs().sum(axis=1) > 1e-9
    return {
        "median_portfolio_share_value": float(portfolio_value[active].median()),
        "median_universe_share_value": float(universe_value[active].median()),
        "portfolio_to_universe_ratio": float(portfolio_value[active].median() / universe_value[active].median()),
        "min_portfolio_share_value": float(portfolio_value[active].min()),
    }


def main() -> int:
    panels = load_eod_panels()
    close = panels["close"]
    universe = load_pit_universe()
    avail = resolve_research_universe(close, universe)
    close = close.loc[:, [c for c in close.columns if c in avail]]
    volume = panels["volume"].loc[:, close.columns].reindex(index=close.index)
    mask = panel_universe_mask(universe, close.index, close.columns)
    config = DiscoveryConfig(rebalance_frequency="M", one_way_cost_bps=12)

    out = {}
    for window, std in ((15, 3.0), (20, 2.5), (30, 2.5), (40, 2.5)):
        w = long_only_weights(close, window, std, 0.25).where(mask, 0.0).apply(_norm_long, axis=1)
        res = simulate(close, w, config=config)
        monthly = _monthly(res.returns)
        worst = monthly.sort_values(ascending=True).head(8)
        best = monthly.sort_values(ascending=False).head(8)
        roll = _rolling_sharpe(res.returns)
        # 126-day rolling sharpe, find worst stretch
        roll_valid = roll.dropna()
        worst_roll_start = roll_valid.idxmin() if len(roll_valid) else None
        pos = _position_stats(w)
        liq = _liquidity_check(w, volume, close)

        # Start-date sensitivity
        start_metrics = {}
        for start in ("2024-01-01", "2025-01-01"):
            sub = close.loc[start:]
            sub_mask = mask.loc[start:]
            sw = long_only_weights(sub, window, std, 0.25).where(sub_mask, 0.0).apply(_norm_long, axis=1)
            sr = simulate(sub, sw, config=config)
            start_metrics[start] = _daily_metrics(sr.returns)

        out[f"boll_{window}_{std}"] = {
            "metrics": _daily_metrics(res.returns),
            "monthly_series": {
                str(k): float(v) for k, v in monthly.items()
            },
            "worst_5_months": {str(k): float(v) for k, v in worst.head(5).items()},
            "best_5_months": {str(k): float(v) for k, v in best.head(5).items()},
            "worst_rolling_sharpe_start": str(worst_roll_start) if worst_roll_start is not None else None,
            "rolling_6m_min_sharpe": float(roll_valid.min()) if len(roll_valid) else None,
            "rolling_6m_max_sharpe": float(roll_valid.max()) if len(roll_valid) else None,
            "position_stats": pos,
            "liquidity_proxy": liq,
            "start_date_metrics": start_metrics,
            "yearly": {
                str(k): float(v)
                for k, v in res.returns.resample("YE").apply(lambda x: (1.0 + x).prod() - 1.0).items()
            },
        }

    path = FEATURES_DIR / "risk_detail.json"
    path.write_text(json.dumps(out, indent=2, default=str))
    print(f"Wrote {path}")

    for key, entry in out.items():
        print(f"\n=== {key} ===")
        m = entry["metrics"]
        print("metrics:", {k: round(v, 4) for k, v in m.items() if isinstance(v, (int, float))})
        print("worst5:", {k: round(v, 4) for k, v in entry["worst_5_months"].items()})
        print("best5:", {k: round(v, 4) for k, v in entry["best_5_months"].items()})
        print("position_stats:", {k: round(v, 3) for k, v in entry["position_stats"].items()})
        print("liquidity:", {k: round(v, 3) for k, v in entry["liquidity_proxy"].items()})
        print("start_metrics:", {k: {kk: round(vv, 3) for kk, vv in v.items() if isinstance(vv, (int, float))} for k, v in entry["start_date_metrics"].items()})
        print("rolling_sharpe:", entry["rolling_6m_min_sharpe"],
              entry["rolling_6m_max_sharpe"], "start", entry["worst_rolling_sharpe_start"])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
