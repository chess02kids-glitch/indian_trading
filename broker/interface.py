"""The unified broker adapter interface.

Every broker integration (sandbox today) implements :class:`BrokerAdapter`.
Research and strategy code must never call broker SDKs or adapters directly;
the only sanctioned order path is::

    Research → Portfolio → OrderIntent → Risk Engine → ExecutionService
        → SandboxExecutionAdapter → BrokerAdapter → sandbox broker

Read operations (profile/funds/holdings/positions/quotes/orders/trades) are
available to operational surfaces (CLI, dashboards) but remain side-effect
free. The only mutating operations are ``place_limit_order`` and
``cancel_order``, and both act on validated domain objects only.
"""

from __future__ import annotations

from abc import ABC, abstractmethod

from broker.models import (
    BrokerOrderRecord,
    BrokerProfile,
    FundsSummary,
    Holding,
    Quote,
    TradeRecord,
)
from models.domain import OrderIntent, Position

__all__ = ["BrokerAdapter"]


class BrokerAdapter(ABC):
    """Common interface for broker adapters (Upstox, Dhan, ...).

    Implementations in this repository are sandbox adapters: they can only
    reach simulated sandbox endpoints. There is no production adapter.
    """

    @property
    @abstractmethod
    def broker_name(self) -> str:
        """Short broker identifier (``"upstox"`` or ``"dhan"``)."""

    # -- authentication -------------------------------------------------

    @abstractmethod
    def login_url(self, state: str) -> str:
        """Return the (sandbox) OAuth authorization URL for manual login."""

    @abstractmethod
    def complete_login(self, code: str) -> object:
        """Exchange a manually obtained code for a token and store it."""

    @abstractmethod
    def is_authenticated(self) -> bool:
        """True when a non-expired token is available locally."""

    # -- account reads ----------------------------------------------------

    @abstractmethod
    def ping(self) -> bool:
        """Lightweight connectivity probe; False when unreachable."""

    @abstractmethod
    def get_profile(self) -> BrokerProfile:
        """Return the authenticated account profile."""

    @abstractmethod
    def get_funds(self) -> FundsSummary:
        """Return available funds and margin utilisation."""

    @abstractmethod
    def get_holdings(self) -> list[Holding]:
        """Return delivery holdings."""

    @abstractmethod
    def get_positions(self) -> list[Position]:
        """Return current net positions as domain ``Position`` objects."""

    @abstractmethod
    def get_quote(self, symbol: str, exchange: str = "NSE") -> Quote:
        """Return the last traded price for one instrument."""

    # -- orders -----------------------------------------------------------

    @abstractmethod
    def place_limit_order(self, intent: OrderIntent) -> BrokerOrderRecord:
        """Place one validated LIMIT order. Never accepts MARKET/IOC."""

    @abstractmethod
    def get_order_status(self, order_ref: str) -> BrokerOrderRecord | None:
        """Return the latest record for a broker order id or internal tag."""

    @abstractmethod
    def cancel_order(self, order_ref: str) -> BrokerOrderRecord | None:
        """Cancel an open order; returns the terminal record or None."""

    @abstractmethod
    def get_trade_history(self) -> list[TradeRecord]:
        """Return the day's executed trades."""
