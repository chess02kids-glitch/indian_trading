"""Tests for the historical universe dataset and its date-safety guards."""

from __future__ import annotations

from datetime import date

import pytest

from data.universe import (
    UniverseDataset,
    UniverseMembership,
    load_universe_dataset,
)
from research import nifty_50, nifty_100
from research.contracts import ResearchInputError
from research.universe import (
    build_universe_from_dataset,
    ensure_universe_period_covers,
)


def _small_dataset() -> UniverseDataset:
    return UniverseDataset(
        [
            UniverseMembership(
                "RELIANCE", "nifty100", date(2020, 1, 1), isin="INE002A01018",
                sector="Energy", exchange="NSE",
            ),
            UniverseMembership(
                "TCS", "nifty100", date(2020, 1, 1), isin="INE467B01029",
                sector="IT", exchange="NSE",
            ),
            # Former constituent with finite valid_to (survivorship-safe).
            UniverseMembership(
                "RCOM", "nifty100", date(2020, 1, 1), date(2022, 6, 30),
                sector="Telecom", exchange="NSE", delisted=True,
            ),
        ]
    )


def test_members_at_resolves_point_in_time() -> None:
    dataset = _small_dataset()
    assert dataset.members_at("nifty100", date(2021, 6, 1)) == (
        "RCOM", "RELIANCE", "TCS",
    )
    # After RCOM left the index it no longer appears.
    assert dataset.members_at("nifty100", date(2023, 1, 1)) == ("RELIANCE", "TCS")


def test_all_symbols_keeps_delisted_names() -> None:
    """Survivorship-bias protection: removed names stay queryable."""
    dataset = _small_dataset()
    assert "RCOM" in dataset.all_symbols("nifty100")
    assert len(dataset.all_symbols("nifty100")) == 3


def test_validate_period_refuses_invalid_dates() -> None:
    dataset = _small_dataset()
    dataset.validate_period("nifty100", date(2020, 1, 1), date(2021, 1, 1))
    with pytest.raises(ValueError, match="no membership before"):
        dataset.validate_period("nifty100", date(2019, 12, 31))
    # A fully closed dataset (no current members) refuses dates after the
    # last recorded removal.
    closed = UniverseDataset(
        [
            UniverseMembership("X", "nifty100", date(2020, 1, 1), date(2021, 6, 30)),
        ]
    )
    with pytest.raises(ValueError, match="no membership after"):
        closed.validate_period("nifty100", date(2021, 7, 1))


def test_from_dir_loads_repository_dataset() -> None:
    dataset = load_universe_dataset()
    assert "nifty100" in dataset.index_names
    assert "nifty50" in dataset.index_names
    assert "nifty500" in dataset.index_names
    # Repository snapshot records at least 100 current Nifty100 names.
    assert len(dataset.all_symbols("nifty100")) >= 100


def test_build_universe_from_dataset() -> None:
    dataset = load_universe_dataset()
    universe = build_universe_from_dataset(dataset, "nifty100")
    assert universe.name == "nifty100"
    assert len(universe.symbols) >= 100
    assert universe.metadata["valid_from"] == "2023-01-01"
    assert "all_symbols_count" in universe.metadata


def test_ensure_universe_period_refuses_early_dates() -> None:
    dataset = load_universe_dataset()
    universe = build_universe_from_dataset(dataset, "nifty100")
    with pytest.raises(ResearchInputError, match="no membership before"):
        ensure_universe_period_covers(universe, date(2000, 1, 1), None)


def test_frozen_snapshot_has_no_validity_window() -> None:
    """Frozen snapshots are single-date views, not historical claims."""
    universe = nifty_100()
    assert "valid_from" not in universe.metadata
    # No guard applies to frozen snapshots.
    ensure_universe_period_covers(universe, date(2000, 1, 1), None)


def test_nifty50_and_nifty100_repository_rows() -> None:
    dataset = load_universe_dataset()
    n50 = nifty_50().symbols
    n100 = nifty_100().symbols
    # The repository dataset covers the frozen snapshot constituents.
    assert set(n50) <= set(dataset.all_symbols("nifty50"))
    assert set(n100) <= set(dataset.all_symbols("nifty100"))


def test_universe_history_property_is_defined() -> None:
    """Universe exposes a survivorship-safe membership history for backtests."""
    universe = nifty_50()
    assert universe.history == [universe.symbols]
    assert isinstance(universe.history, list)
