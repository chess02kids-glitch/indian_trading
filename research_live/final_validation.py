"""Final institutional validation of the Momentum + Market-Regime strategy
(lb=20, ma=100, tn=20, hold=20). Produces equity/drawdown curves, bootstrap
confidence intervals, deflated Sharpe, cost sensitivity, and benchmark-relative
yearly returns.
"""
from __future__ import annotations

import sys

import numpy as np
import pandas as pd

sys.path.insert(0, "/home/user/indian_trading")
from research_live.broad_data import load_broad_universe
from research_live.metrics import Metrics, deflated_sharpe
from research_live.validate import build_wide, strategy_tgt, sim

COST = 0.0015
S, E = "2010-01-01", "2026-06-30"
IS_S, IS_E = "2010-01-01", "2018-12-31"
OOS_S, OOS_E = "2019-01-01", "2026-06-30"
CFG = dict(lookback=20, hold=20, top_n=20, ma=100)


def main():
    uni = load_broad_universe(min_years=8, min_avg_value=5e6, start="2010-01-01")
    close, high, low, open_ = build_wide(uni, S, E)
    ret = close.pct_change().fillna(0.0)

    tgt = strategy_tgt(close, **CFG).shift(1).fillna(0.0)
    pr, eq = sim(ret, tgt)
    m_full = Metrics.from_returns(pr, eq)
    m_full.turnover = float(pr.abs().mean())  # proxy; use real below

    idx_is = pr.index <= pd.Timestamp(IS_E)
    idx_oos = pr.index >= pd.Timestamp(OOS_S)
    m_is = Metrics.from_returns(pr[idx_is], eq[idx_is])
    m_oos = Metrics.from_returns(pr[idx_oos], eq[idx_oos])

    print("========== Strategy Card Metrics (lb=20, ma=100, tn=20, hold=20) ==========")
    for label, m in [("FULL", m_full), ("IS 2010-18", m_is), ("OOS 2019-26", m_oos)]:
        print(f"\n--- {label} ---")
        for k in ["cagr", "ann_ret", "total_ret", "sharpe", "sortino", "calmar",
                  "max_dd", "profit_factor", "win_rate", "vol", "recovery_factor",
                  "annual_turnover"]:
            v = getattr(m, k)
            print(f"  {k:16s}: {v:.3f}" if isinstance(v, float) else f"  {k:16s}: {v}")

    # ---- Bootstrap CI on daily returns (OOS) ----
    rng = np.random.default_rng(42)
    oos_ret = pr[idx_oos].values
    boots = []
    for _ in range(2000):
        b = rng.choice(oos_ret, size=len(oos_ret), replace=True)
        boots.append(b.mean() / b.std() * np.sqrt(252) if b.std() > 0 else 0)
    boots = np.array(boots)
    print(f"\nBootstrap OOS Sharpe: mean={boots.mean():.2f} "
          f"95% CI=[{np.percentile(boots,2.5):.2f}, {np.percentile(boots,97.5):.2f}]")
    print(f"  P(Sharpe>0) = {(boots>0).mean():.1%}")

    # ---- Deflated Sharpe (surviving after many trials) ----
    n_trials = 200  # number of independent configs explored in this research
    dsh = deflated_sharpe(m_oos.sharpe, int(idx_oos.sum()), n_trials)
    print(f"Deflated Sharpe (OOS, {n_trials} trials): {dsh:.3f}")

    # ---- Cost sensitivity ----
    print("\n---- Cost sensitivity (OOS) ----")
    for mult in [1.0, 2.0, 3.0]:
        prc, eqc = sim(ret, tgt, cost_mult=mult)
        io = prc.index >= pd.Timestamp(OOS_S)
        mc = Metrics.from_returns(prc[io], eqc[io])
        print(f"  cost x{mult:.0f}: OOS Sharpe={mc.sharpe:.2f} CAGR={mc.cagr:.2f}")

    # ---- Benchmark comparison (broad EW) yearly ----
    print("\n---- Yearly returns: Strategy vs Broad EW Benchmark ----")
    n = close.notna().sum(axis=1).replace(0, np.nan)
    ew_w = close.notna().astype(float).div(n, axis=0).fillna(0)
    pr_ew = (ew_w * ret).sum(axis=1)
    eq_ew = (1 + pr_ew).cumprod()
    years = range(2010, 2027)
    for y in years:
        st, en = f"{y}-01-01", f"{y}-12-31"
        idx = (pr.index >= st) & (pr.index <= en)
        ys = (1 + pr[idx]).prod() - 1
        ye = (1 + pr_ew[idx]).prod() - 1
        flag = "+" if ys > ye else "-"
        print(f"  {y}: strat={ys*100:6.1f}%  EW={ye*100:6.1f}%  out{flag}")

    # ---- Save curves ----
    eq.to_csv("research_live/strategy_equity.csv", header=["equity"])
    dd = eq / eq.cummax() - 1
    dd.to_csv("research_live/strategy_drawdown.csv", header=["dd"])


if __name__ == "__main__":
    main()
