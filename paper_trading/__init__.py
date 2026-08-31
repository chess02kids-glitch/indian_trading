"""Local, data-only paper trading.

This package deliberately separates an Upstox *market-data* connection from
our virtual portfolio.  It has no broker-order code, and therefore cannot
submit an order to an Upstox account.
"""

from .ledger import PaperLedger
from .market_data import MarketDataUnavailable, MarketQuote, UpstoxMarketData
from .service import PaperTradingService

__all__ = [
    "MarketDataUnavailable",
    "MarketQuote",
    "PaperLedger",
    "PaperTradingService",
    "UpstoxMarketData",
]
