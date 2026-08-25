"""Broker sandbox layer.

A unified broker adapter abstraction with sandbox-only implementations for
Upstox and Dhan, a safe execution layer (LIMIT-only enforcement, rate
limiting, duplicate prevention, token expiry gates), deterministic failure
injection, and sandbox reconciliation. There is deliberately no production
adapter and no live order path: ``LIVE`` mode refuses everywhere.

Public surface::

    from broker import (
        OperatingMode, resolve_operating_mode, check_execution_permitted,
        BrokerAdapter, UpstoxAdapter, DhanAdapter, create_adapter,
        TokenManager, FileTokenStore,
        RateLimiter, call_with_retries,
        SimulatedSandboxTransport, SandboxExecutionAdapter, SandboxReconciler,
    )
"""

from __future__ import annotations

from broker.adapter import (
    SUPPORTED_BROKERS,
    BaseSandboxAdapter,
    BrokerAdapter,
    DhanAdapter,
    UpstoxAdapter,
    create_adapter,
)
from broker.errors import (
    BrokerAuthenticationError,
    BrokerConfigurationError,
    BrokerError,
    BrokerRejectedOrderError,
    BrokerResponseError,
    BrokerTransportError,
    LiveTradingDisabledError,
    SandboxOnlyError,
    StaleTokenError,
)
from broker.mode import (
    OperatingMode,
    check_execution_permitted,
    resolve_operating_mode,
    to_execution_mode,
)
from broker.rate_limit import RateLimiter, call_with_retries
from broker.reconciler import SandboxReconciler, record_to_result
from broker.safe_execution import SandboxExecutionAdapter
from broker.simulated import (
    BEHAVIOURAL_FAULTS,
    TRANSPORT_FAULTS,
    DisconnectFault,
    Fault,
    PartialFillFault,
    PendingFault,
    RejectFault,
    SimulatedBrokerBackend,
    StaleTokenFault,
    TimeoutFault,
)
from broker.token import FileTokenStore, TokenManager, TokenRecord, TokenStatus
from broker.transport import (
    HttpSandboxTransportStub,
    SimulatedSandboxTransport,
    validate_sandbox_base_url,
)

__all__ = [
    # adapters
    "BrokerAdapter",
    "BaseSandboxAdapter",
    "UpstoxAdapter",
    "DhanAdapter",
    "SUPPORTED_BROKERS",
    "create_adapter",
    # modes / feature flags
    "OperatingMode",
    "resolve_operating_mode",
    "check_execution_permitted",
    "to_execution_mode",
    # token management
    "TokenManager",
    "FileTokenStore",
    "TokenRecord",
    "TokenStatus",
    # rate limiting / retry
    "RateLimiter",
    "call_with_retries",
    # transports + simulated backend
    "SimulatedSandboxTransport",
    "HttpSandboxTransportStub",
    "validate_sandbox_base_url",
    "SimulatedBrokerBackend",
    # failure injection
    "Fault",
    "TimeoutFault",
    "DisconnectFault",
    "StaleTokenFault",
    "RejectFault",
    "PartialFillFault",
    "PendingFault",
    "TRANSPORT_FAULTS",
    "BEHAVIOURAL_FAULTS",
    # execution + reconciliation
    "SandboxExecutionAdapter",
    "SandboxReconciler",
    "record_to_result",
    # errors
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
