import os

import pytest

from store.supabase import SupabaseDatasetRepository, SupabaseUniverseRepository


@pytest.mark.skipif(
    not os.getenv("DATABASE_URL"), reason="Requires Supabase DATABASE_URL"
)
def test_dataset_persistence_idempotency():
    repo = SupabaseDatasetRepository()

    # Save first time
    repo.save_dataset_metadata("test_dataset", "abcd_1234_efgh", {"rows": 100})

    # Save second time should not raise unique constraint error
    repo.save_dataset_metadata("test_dataset", "abcd_1234_efgh", {"rows": 100})


@pytest.mark.skipif(
    not os.getenv("DATABASE_URL"), reason="Requires Supabase DATABASE_URL"
)
def test_universe_history_persistence():
    repo = SupabaseUniverseRepository()
    repo.save_universe_history("RELIANCE", "NIFTY50", "2024-01-01", "2024-12-31")
    # If no exception, successful
