"""Adversarial validation for the top discovery candidates.

Checks:
* Trades only at month-end, next-day execution (no same-bar look-ahead).
* Reports gross vs net return and turnover/cost drag.
* Splits in-sample (2023-01..2024-12) and out-of-sample (2025-01..2026-08).
* Walk-forward rolling blocks.
* Parameter sensitivity around the headline configuration.
* Annual/yearly decomposition.
* Concentration / top-name contribution.
* Benchmark relative (equal-weight 126-panel buy-and-hold is the beta reference).

Outputs machine-readable JSON to ``data/features/validation_report.json``.
"""

from __future__ import annotations

import itertools
import json
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
from .strategies import build_specs

ROOT = Path(__file__).resolve().parents[2]
FEATURES_DIR = ROOT / "data" / "features"


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
    dd = equity / equity.cummax() - 1.0
    return {
        "total_return": total,
        "cagr": ann,
        "volatility": vol,
        "sharpe": sharpe,
        "max_drawdown": float(dd.min()),
        "periods": float(n),
    }


def _turnover_stats(result, cost_rate: float) -> dict[str, float]:
    turnover = result.turnover
    rebalance_dates = turnover[turnover > 0]
    avg_turn = float(rebalance_dates.mean()) if len(rebalance_dates) else 0.0
    total_turn = float(turnover.sum())
    total_cost = float((turnover * cost_rate).sum())
    gross_return = (1.0 + result.returns).prod() - 1.0
    net = result.metrics.get("total_return", 0.0)
    return {
        "rebalance_count": int(len(rebalance_dates)),
        "avg_turnover": avg_turn,
        "total_turnover": total_turn,
        "total_cost": total_cost,
        "gross_return": gross_return,
        "net_return": net,
        "cost_drag_pct_of_gross": (total_cost / abs(gross_return) * 100.0)
        if gross_return != 0
        else 0.0,
    }


def _yearly(returns: pd.Series) -> dict[str, float]:
    out = {}
    for year, sub in returns.groupby(returns.index.year):
        sub_eq = (1.0 + sub).cumprod()
        out[str(year)] = float(sub_eq.iloc[-1] - 1.0)
    return out


def _walk_forward(
    prices: pd.DataFrame,
    weights: pd.DataFrame,
    masks: dict[str, pd.DataFrame],
    config: DiscoveryConfig,
    block_months: int,
) -> dict[str, float]:
    """Roll through non-overlapping blocks measuring Sharpe / return each block."""
    idx = prices.index
    periods = idx.to_period("M")
    months = sorted(set(periods))
    rows = []
    for i in range(0, len(months), block_months):
        start_m = months[i]
        end_m = months[min(i + block_months - 1, len(months) - 1)]
        mask = (periods >= start_m) & (periods <= end_m)
        sub_idx = idx[mask]
        sub_prices = prices.loc[sub_idx]
        # pick weights sampled at each month end inside block (same as engine)
        res = simulate(sub_prices, weights.loc[sub_idx], config=config)
        rows.append(
            {
                "start": start_m.strftime("%Y-%m"),
                "end": end_m.strftime("%Y-%m"),
                **_daily_metrics(res.returns),
            }
        )
    if not rows:
        return {}
    sharpe = [r["sharpe"] for r in rows]
    return {
        "blocks": len(rows),
        "mean_block_sharpe": float(np.mean(sharpe)),
        "min_block_sharpe": float(np.min(sharpe)),
        "max_block_sharpe": float(np.max(sharpe)),
        "positive_blocks": int(sum(s > 0 for s in sharpe)),
        "block_details": rows,
    }


def _parameter_sensitivity(
    family: str,
    params: dict[str, list[float]],
    close: pd.DataFrame,
    load_weights,
    config: DiscoveryConfig,
    mask: pd.DataFrame,
) -> list[dict[str, object]]:
    rows = []
    keys = list(params)
    grids = [params[k] for k in keys]
    for combo in itertools.product(*grids):
        p = dict(zip(keys, combo))
        try:
            w = load_weights(close, p)
            w = w.where(mask, 0.0)
            # long-only normalize
            def norm(row):
                s = row.sum()
                if s > 0 and (row >= 0).all():
                    return row / s
                return row

            w = w.apply(norm, axis=1)
            res = simulate(close, w, config=config)
            rows.append({**p, **res.metrics})
        except Exception as exc:
            rows.append({**p, "error": f"{type(exc).__name__}: {exc}"})
    return rows


def main() -> int:
    print("Loading panels ...")
    panels = load_eod_panels()
    close = panels["close"]
    universe = load_pit_universe()
    avail = resolve_research_universe(close, universe)
    close = close.loc[:, [c for c in close.columns if c in avail]]
    panels = {k: v.loc[:, close.columns].reindex(index=close.index) for k, v in panels.items()}
    mask = panel_universe_mask(universe, close.index, close.columns)

    config = DiscoveryConfig(rebalance_frequency="M", one_way_cost_bps=12.0)
    cost_rate = config.one_way_cost_bps / 10_000.0

    specs = {s.name: s for s in build_specs()}

    candidate_names = [
        "boll_30_2.5",
        "rsi_14_30",
        "invvol_63d",
        "invvol_20d",
        "ts_mom_20d",
        "ma_trend_50_200",
        "xs_mom_20d",
        "xs_mom_63d",
        "lowvol_20d",
        "equal_weight",
        "buy_and_hold",
    ]

    report: dict[str, object] = {"config": config.__dict__, "candidates": {}}

    for name in candidate_names:
        spec = specs[name]
        all_w = spec.generate(panels).where(mask, 0.0)

        def norm(row):
            s = row.sum()
            if s > 0 and (row >= 0).all():
                return row / s
            return row

        all_w = all_w.apply(norm, axis=1)
        full = simulate(close, all_w, config=config)
        turnover = _turnover_stats(full, cost_rate)

        train = close.loc["2023-01-01":"2024-12-31"]
        test = close.loc["2025-01-01":"2026-08-31"]
        train_res = simulate(train, all_w.loc[train.index], config=config)
        test_res = simulate(test, all_w.loc[test.index], config=config)

        block_months = 6
        wf = _walk_forward(close, all_w, {}, config, block_months)

        spec_entry = {
            "family": spec.family,
            "parameters": dict(spec.parameters),
            "full": full.metrics,
            "turnover": turnover,
            "train_2023_2024": train_res.metrics,
            "test_2025_2026": test_res.metrics,
            "yearly": _yearly(full.returns),
            "walk_forward_6m": wf,
        }

        # Concentration / contribution: top 10 positions by average weight.
        avg_w = full.weights.mean().sort_values(ascending=False).head(10)
        spec_entry["top_mean_weights"] = avg_w.to_dict()

        # For a longer-term trade, track sign of variable; all are tested on
        # the full plus the split, so we have enough for discovery.
        report["candidates"][name] = spec_entry

    # Parameter sensitivity for the two strongest mean-reversion families.
    sensitivity: dict[str, list[dict[str, object]]] = {}

    def boll_load(close_df, p):
        from .strategies import _bollinger_weights

        return _bollinger_weights(close_df, int(p["window"]), float(p["std"]), 0.25)

    sensitivity["bollinger"] = _parameter_sensitivity(
        "bollinger",
        {"window": [15, 20, 30, 40, 60], "std": [1.5, 2.0, 2.5, 3.0]},
        close,
        boll_load,
        config,
        mask,
    )

    def rsi_load(close_df, p):
        from .strategies import _rsi2_weights

        return _rsi2_weights(close_df, int(p["window"]), float(p["threshold"]), 0.25)

    sensitivity["rsi"] = _parameter_sensitivity(
        "rsi",
        {"window": [2, 5, 14, 21], "threshold": [20.0, 25.0, 30.0, 35.0]},
        close,
        rsi_load,
        config,
        mask,
    )

    report["parameter_sensitivity"] = sensitivity

    path = FEATURES_DIR / "validation_report.json"
    path.write_text(json.dumps(report, indent=2, default=str))
    print(f"Wrote {path}")
    print("\n=== candidate summary (full period) ===")
    table = []
    for name, entry in report["candidates"].items():
        table.append(
            {
                "name": name,
                "family": entry["family"],
                "cagr": entry["full"].get("cagr"),
                "vol": entry["full"].get("volatility"),
                "sharpe": entry["full"].get("sharpe"),
                "mdd": entry["full"].get("max_drawdown"),
                "turnover": entry["turnover"]["avg_turnover"],
                "cost_drag": entry["turnover"]["cost_drag_pct_of_gross"],
                "test_sharpe": entry["test_2025_2026"].get("sharpe"),
                "test_cagr": entry["test_2025_2026"].get("cagr"),
                "wf_mean_sharpe": entry["walk_forward_6m"].get("mean_block_sharpe"),
                "wf_pos_blocks": entry["walk_forward_6m"].get("positive_blocks"),
            }
        )
    df = pd.DataFrame(table).sort_values("sharpe", ascending=False)
    pd.set_option("display.width", 250)
    print(df.to_string(index=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
