"""Broker-layer exception hierarchy.

All broker adapters (sandbox or otherwise) raise only these exceptions.
Delivery of an order to a broker can never raise an unclassified error out
of the adapter boundary; every failure mode is one of the types below so
the safe-execution layer can map it to a deterministic ``OrderResult``.
"""

from __future__ import annotations

__all__ = [
    "BrokerError",
    "BrokerConfigurationError",
    "LiveTradingDisabledError",
    "SandboxOnlyError",
    "BrokerAuthenticationError",
    "StaleTokenError",
    "BrokerTransportError",
    "BrokerRejectedOrderError",
    "BrokerResponseError",
]


class BrokerError(RuntimeError):
    """Base class for every broker-layer failure."""


class BrokerConfigurationError(BrokerError):
    """Raised when adapter configuration is unsafe or incomplete."""


class LiveTradingDisabledError(BrokerError):
    """Raised whenever a code path would reach production capital.

    Live execution is outside the system boundary. This error is raised by
    construction whenever any component is configured to reach a real
    (non-sandbox) broker endpoint, and at submission time if an adapter is
    ever put into a live mode.
    """


class SandboxOnlyError(BrokerError):
    """Raised when a sandbox adapter is used outside SANDBOX mode."""


class BrokerAuthenticationError(BrokerError):
    """Raised when the broker rejects the access token (HTTP 401 analogue)."""


class StaleTokenError(BrokerAuthenticationError):
    """Raised when the locally stored token is missing or expired."""


class BrokerTransportError(BrokerError):
    """Raised for transport-level failures: timeouts and disconnects."""


class BrokerRejectedOrderError(BrokerError):
    """Raised when the broker refuses an order-level request (e.g. cancel)."""


class BrokerResponseError(BrokerError):
    """Raised when a broker response cannot be interpreted deterministically."""
