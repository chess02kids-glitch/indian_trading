import time
import sys
sys.path.insert(0, "/home/user/indian_trading")
from research_live.data import load_panel, liquid_universe, align_panel
from research_live.runner import StrategyRunner
import research_live.strategies as S
import pandas as pd

t0 = time.time()
panel = load_panel()
print("loaded panel", time.time() - t0, "s")
syms = liquid_universe(panel, start="2008-01-01", min_frac=0.9)
print("liquid universe", len(syms))
sub, close = align_panel(panel, syms, start="2009-01-01", end="2026-06-30")
high = sub["high"].unstack("symbol")
low = sub["low"].unstack("symbol")
opn = sub["open"].unstack("symbol")
print("close shape", close.shape, "range", close.index.min(), close.index.max())

runner = StrategyRunner(sub, close, high, low, opn, cost_oneway=0.0015)

# quick tests
for name, fn, kw in [
    ("dual_ma", S.strat_dual_ma, dict(fast=20, slow=100)),
    ("rsi_rev", S.strat_rsi_rev, dict(n=14, lo=30, hi=50)),
    ("donchian", S.strat_donchian, dict(n=50, atr_stop=2.0)),
]:
    t1 = time.time()
    m, res = runner.evaluate(fn, **kw)
    print(f"{name}: cagr={m.cagr:.3f} sharpe={m.sharpe:.2f} mdd={m.max_dd:.2f} "
          f"pf={m.profit_factor:.2f} turn={m.annual_turnover:.1f} [{time.time()-t1:.1f}s]")
