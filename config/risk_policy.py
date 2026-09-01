"""One place where the operator's risk limits are defined.

AUDIT-024 / AUDIT-029
=====================

Before this module the repository carried **two independent, conflicting
sets of risk limits** with no statement about which governed:

* :class:`risk_kill.guard.RiskLimits` — max position 25\%, gross 100\%,
  daily loss 3\%, drawdown **10\%**, data age **18 hours**;
* ``paper_trading.DEFAULT_RISK_POLICY`` — max position **15\%**, gross
  100\%, daily loss 3\%, drawdown **15\%**, and
  ``data.quality.detect_data_staleness`` allowed data **6 days** old.

So the paper account would happily hold a 15\%-of-book position that the
risk guard would reject at 25\% *and* accept a 15\% drawdown the guard
would already have locked the account for, while the data layer blessed
prices eight times staler than the guard was willing to trade on.

The rule now:

1. :class:`risk_kill.guard.RiskLimits` stays the **single source of
   truth** — it is the safety-critical layer and deliberately depends on
   nothing but the standard library, so it cannot be imported *by* this
   module in a way that creates a cycle.
2. :mod:`config.risk_policy` derives every other limit from it, taking the
   **more conservative** value wherever another layer had a tighter
   bound. Nothing here loosens a limit.
3. The data-quality staleness window is expressed here too, next to the
   trading window, so the two can be read side by side instead of drifting
   apart in separate files.

Behavioural change to be aware of
---------------------------------
The paper account's drawdown limit moves from 15\% to **10\%** (the guard's
value, i.e. stricter). The paper max-position limit stays 15\% (stricter
than the guard's 25\%), so nothing is loosened anywhere. Any paper run that
was relying on a 15\% drawdown before locking will now lock earlier.
"""

from __future__ import annotations

from risk_kill.guard import RiskLimits

# The canonical limits. Constructing the default is cheap and stateless.
CANONICAL_LIMITS = RiskLimits()

# -- Trading (risk_kill guard) ----------------------------------------------

#: How old a price may be before the guard refuses to trade on it. This is
#: an *intraday* tolerance: it is checked against a live quote timestamp.
MAX_DATA_AGE_HOURS: float = CANONICAL_LIMITS.max_data_age_hours

# -- Data quality (ingestion / EOD completeness) ----------------------------

#: How old the newest bar in a **daily EOD series** may be before the data
#: layer flags it. This is deliberately *not* the same number as
#: ``MAX_DATA_AGE_HOURS``: a daily series legitimately sits untouched across
#: a weekend and an NSE holiday, and flagging a Monday-morning run for
#: holding Friday's close would be a false positive. It governs "is this
#: history complete?", not "may I trade on this quote right now?".
#:
#: The relationship is asserted in :func:`assert_quality_window_is_consistent`
#: so the two numbers cannot silently drift apart again.
MAX_DATA_AGE_QUALITY_DAYS: float = 6.0


def assert_quality_window_is_consistent() -> None:
    """Fail loudly if the two staleness windows contradict each other.

    The quality window is allowed to be *longer* than the trading window
    (completeness is a looser question than tradability), but it must never
    be shorter, and it must never be so long that data the guard would
    refuse is reported as clean for weeks.
    """
    if MAX_DATA_AGE_QUALITY_DAYS * 24.0 < MAX_DATA_AGE_HOURS:
        raise ValueError(
            "data-quality staleness window "
            f"({MAX_DATA_AGE_QUALITY_DAYS}d) is shorter than the trading "
            f"window ({MAX_DATA_AGE_HOURS}h); clean data would still be "
            "untradable"
        )
    if MAX_DATA_AGE_QUALITY_DAYS > 7.0:
        raise ValueError(
            "data-quality staleness window "
            f"({MAX_DATA_AGE_QUALITY_DAYS}d) exceeds one week: data this "
            "old is not 'stale', it is missing"
        )


# -- Paper (virtual account) -------------------------------------------------

#: The paper account is *virtual*, but it exists to predict how the real
#: thing behaves, so it must not run with looser limits. Each value below is
#: the more conservative of the guard's limit and the historical paper
#: policy.
PAPER_MAX_POSITION_WEIGHT: float = min(
    0.15, CANONICAL_LIMITS.max_position_exposure
)
PAPER_MAX_GROSS_EXPOSURE: float = CANONICAL_LIMITS.max_gross_exposure
PAPER_DAILY_LOSS_LIMIT: float = CANONICAL_LIMITS.max_daily_loss
PAPER_MAX_DRAWDOWN: float = min(0.15, CANONICAL_LIMITS.max_drawdown)

#: Paper-only: how many virtual orders one rebalance may create. The guard
#: counts orders per *hour*; a rebalance is a single event, so the paper
#: layer keeps its own, tighter bound.
PAPER_MAX_ORDERS_PER_REBALANCE: int = 30

DEFAULT_RISK_POLICY: dict[str, float | int] = {
    "max_position_weight": PAPER_MAX_POSITION_WEIGHT,
    "max_gross_exposure": PAPER_MAX_GROSS_EXPOSURE,
    "daily_loss_limit": PAPER_DAILY_LOSS_LIMIT,
    "max_drawdown": PAPER_MAX_DRAWDOWN,
    "max_orders_per_rebalance": PAPER_MAX_ORDERS_PER_REBALANCE,
}
