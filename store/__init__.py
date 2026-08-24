"""Persistence interfaces and backends (in-memory, SQLite).

Agent 2 implements Supabase adapters against the same protocols.
"""

from .memory import (
    InMemoryOrderRepository,
    InMemoryPositionRepository,
    InMemoryReconciliationRepository,
    InMemoryResearchRepository,
    InMemoryRunRepository,
)
from .protocols import (
    OrderRepository,
    PositionRepository,
    ReconciliationRepository,
    ResearchRepository,
    RunRepository,
)
from .sqlite import SQLiteStore

__all__ = [
    "InMemoryOrderRepository",
    "InMemoryPositionRepository",
    "InMemoryReconciliationRepository",
    "InMemoryResearchRepository",
    "InMemoryRunRepository",
    "OrderRepository",
    "PositionRepository",
    "ReconciliationRepository",
    "ResearchRepository",
    "RunRepository",
    "SQLiteStore",
]
