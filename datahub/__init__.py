"""datahub — the single source of truth for market data and system state.

Every page in the unified dashboard (Strategy, Live, Paper, Research,
Operations) reads price data, data-health, and heartbeats from *this*
package.  Before datahub existed, the Research Cockpit looked for
``data/clean/prices.parquet``, the Strategy Dashboard read
``data/clean/eod2_data/*.parquet`` directly, and the Live Terminal read
``data/eod2/daily/*.csv`` — so three pages could (and did) disagree about
whether the system had data at all.

Public API
----------
Price data
    :func:`load_panel`         long-form (date, symbol) OHLCV panel
    :func:`wide`               one OHLCV field as a symbol x date matrix
    :func:`select_universe`    honest, recency-aware universe selection
    :func:`strategy_frame`     the MomReM universe close matrix + metadata
    :func:`materialize_prices` write the canonical ``prices.parquet`` bundle
    :func:`data_status`        ONE status dict shared by every page

Quotes
    :class:`QuoteProvider`     UPSTOX -> SIM -> EOD fallback chain

Analytics
    :mod:`datahub.analytics`   regime, divergence, cost sensitivity,
                               strategy correlation, position sizing
    :mod:`datahub.universe`    expand the universe from the raw EOD mirror

State
    :mod:`datahub.state`       heartbeats + kill switch (``var/system_state.json``)
"""

from __future__ import annotations

from datahub.panel import (
    BUNDLE_DIR,
    PRICES_FILE,
    RAW_EOD_DIR,
    cache_path,
    clear_cache,
    data_status,
    ingest_freshness,
    load_panel,
    materialize_prices,
    select_universe,
    strategy_frame,
    wide,
)
from datahub.quotes import (
    EodQuoteProvider,
    QuoteChain,
    QuoteProvider,
    QuoteResult,
    SimQuoteProvider,
    UpstoxQuoteProvider,
    build_quote_chain,
)

__all__ = [
    "BUNDLE_DIR",
    "PRICES_FILE",
    "RAW_EOD_DIR",
    "cache_path",
    "clear_cache",
    "data_status",
    "ingest_freshness",
    "load_panel",
    "materialize_prices",
    "select_universe",
    "strategy_frame",
    "wide",
    "QuoteProvider",
    "QuoteResult",
    "QuoteChain",
    "UpstoxQuoteProvider",
    "SimQuoteProvider",
    "EodQuoteProvider",
    "build_quote_chain",
]
