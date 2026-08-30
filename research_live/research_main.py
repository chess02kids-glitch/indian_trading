"""Main research harness: grid search over strategy families with IS/OOS split
and walk-forward validation, benchmarked against the equal-weight market.

Usage: python research_live/research_main.py [--family FAMILY] [--quick]
"""
from __future__ import annotations

import argparse
import itertools
import json
import sys
import time

import numpy as np
import pandas as pd

sys.path.insert(0, "/home/user/indian_trading")
from research_live.data import load_panel, liquid_universe, align_panel
from research_live.runner import StrategyRunner
from research_live.alpha import benchmark_returns, capm_alpha_beta
import research_live.strategies as S

RF = 0.06
COST = 0.0015  # one-way all-in cost (brokerage+STT+slippage)

IS_START, IS_END = "2009-01-01", "2019-12-31"
OOS_START, OOS_END = "2020-01-01", "2026-06-30"


def load():
    panel = load_panel()
    syms = liquid_universe(panel, start="2008-01-01", min_frac=0.9)
    sub, close = align_panel(panel, syms, start="2009-01-01", end="2026-06-30")
    high = sub["high"].unstack("symbol")
    low = sub["low"].unstack("symbol")
    opn = sub["open"].unstack("symbol")
    runner = StrategyRunner(sub, close, high, low, opn, cost_oneway=COST)
    return panel, runner, close


FAMILIES = {
    "dual_ma": dict(fn=S.strat_dual_ma, ls=False, grid=dict(fast=[10,20,50], slow=[50,100,200])),
    "ma_cross": dict(fn=S.strat_ma_cross, ls=False, grid=dict(fast=[10,20,50], slow=[50,100,200])),
    "rsi_rev": dict(fn=S.strat_rsi_rev, ls=False, grid=dict(n=[3,7,14], lo=[20,30,40])),
    "bollinger_rev": dict(fn=S.strat_bollinger_rev, ls=False, grid=dict(n=[10,20,30], k=[1.5,2.0,2.5])),
    "donchian": dict(fn=S.strat_donchian, ls=False, grid=dict(n=[20,50,100])),
    "ts_momentum": dict(fn=S.strat_ts_momentum, ls=False, grid=dict(lookback=[60,120,250])),
    "momentum_cs_ls": dict(fn=S.strat_momentum_cs_ls, ls=True,
                           grid=dict(lookback=[120,250], hold=[20], frac=[0.2])),
    "reversal_cs_ls": dict(fn=S.strat_reversal_cs_ls, ls=True,
                           grid=dict(lookback=[5,10,20], hold=[5,10], frac=[0.2])),
    "momentum_cs_ls_vol": dict(fn=S.strat_momentum_cs_ls_vol, ls=True,
                               grid=dict(lookback=[250], hold=[20], frac=[0.2])),
}


def run_family(fn, grid, runner, close, ps, pe, long_short):
    keys = list(grid.keys())
    results = []
    combos = list(itertools.product(*grid.values()))
    for combo in combos:
        kw = dict(zip(keys, combo))
        m_is, eq_is, ret_is = runner.run_period(fn, ps, pe, long_short=long_short, **kw)
        results.append(dict(params=kw, is_metrics=m_is.to_dict(), eq_is=eq_is, ret_is=ret_is))
    results.sort(key=lambda r: r["is_metrics"]["sharpe"], reverse=True)
    return results, combos


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--family", default=None)
    ap.add_argument("--quick", action="store_true")
    args = ap.parse_args()

    t0 = time.time()
    panel, runner, close = load()
    print(f"[data] {close.shape[1]} names x {len(close)} days in {time.time()-t0:.1f}s")
    mkt = benchmark_returns(close)

    fams = FAMILIES if not args.family else {args.family: FAMILIES[args.family]}
    all_report = {}
    for fname, spec in fams.items():
        print(f"\n========== {fname} ({'LS' if spec['ls'] else 'long-only'}) ==========")
        grid = {k: (v[:2] if args.quick else v) for k, v in spec["grid"].items()}
        results, combos = run_family(spec["fn"], grid, runner, close, IS_START, IS_END, spec["ls"])
        top = results[:5]
        for r in top:
            m = r["is_metrics"]
            print(f"  {r['params']} IS sh={m['sharpe']:.2f} cagr={m['cagr']:.2f} "
                  f"mdd={m['max_dd']:.2f} pf={m['profit_factor']:.2f} turn={m['annual_turnover']:.0f}")
        oos_list = []
        for r in top:
            kw = r["params"]
            m_oos, eq_oos, ret_oos = runner.run_period(spec["fn"], OOS_START, OOS_END,
                                                        long_short=spec["ls"], **kw)
            a, b, ir = capm_alpha_beta(ret_oos, mkt.loc[OOS_START:OOS_END])
            oos_list.append(dict(params=kw, metrics=m_oos.to_dict(), alpha=a, beta=b, ir=ir))
        all_report[fname] = dict(top_is=[r["is_metrics"] for r in top], oos=oos_list)

    print("\n===== OOS SUMMARY (top config) =====")
    for fname, rep in all_report.items():
        if not rep["oos"]:
            continue
        o = rep["oos"][0]; i = rep["top_is"][0]
        print(f"{fname}: IS sh={i['sharpe']:.2f} | OOS sh={o['metrics']['sharpe']:.2f} "
              f"cagr={o['metrics']['cagr']:.2f} mdd={o['metrics']['max_dd']:.2f} "
              f"pf={o['metrics']['profit_factor']:.2f} alpha={o['alpha']:.2f} "
              f"beta={o['beta']:.2f} ir={o['ir']:.2f} turn={o['metrics']['annual_turnover']:.0f}")
    with open("research_live/results_summary.json", "w") as f:
        json.dump(all_report, f, default=str)


if __name__ == "__main__":
    main()
