from datetime import date

import pandas as pd
import pytest

from research.corporate_actions import (
    CorporateAction,
    CorporateActionType,
    UnknownCorporateActionError,
    apply_corporate_actions,
    apply_delistings,
    apply_renames,
    audit_corporate_action_coverage,
    verify_corporate_actions,
)


def test_corporate_action_split():
    dates = pd.date_range("2023-01-01", "2023-01-05")
    prices = pd.DataFrame({"RELIANCE": [100.0, 100.0, 100.0, 50.0, 50.0]}, index=dates)

    action = CorporateAction(
        symbol="RELIANCE",
        ex_date=date(2023, 1, 4),
        action_type=CorporateActionType.SPLIT,
        ratio=0.5,
    )

    adjusted = apply_corporate_actions(prices, [action])

    # Verify raw prices are untouched
    assert prices.loc["2023-01-01", "RELIANCE"] == 100.0
    assert prices.loc["2023-01-04", "RELIANCE"] == 50.0

    # Verify adjusted prices
    assert adjusted.loc["2023-01-01", "RELIANCE"] == 50.0
    assert adjusted.loc["2023-01-03", "RELIANCE"] == 50.0
    assert adjusted.loc["2023-01-04", "RELIANCE"] == 50.0
    assert adjusted.loc["2023-01-05", "RELIANCE"] == 50.0


def test_corporate_action_dividend():
    dates = pd.date_range("2023-01-01", "2023-01-05")
    prices = pd.DataFrame({"INFY": [150.0, 150.0, 150.0, 140.0, 140.0]}, index=dates)

    action = CorporateAction(
        symbol="INFY",
        ex_date=date(2023, 1, 4),
        action_type=CorporateActionType.DIVIDEND,
        amount=10.0,
    )

    adjusted = apply_corporate_actions(prices, [action])

    assert adjusted.loc["2023-01-01", "INFY"] == 140.0
    assert adjusted.loc["2023-01-03", "INFY"] == 140.0
    assert adjusted.loc["2023-01-04", "INFY"] == 140.0


def test_corporate_action_validation():
    with pytest.raises(ValueError):
        CorporateAction(
            symbol="TCS",
            ex_date=date(2023, 1, 1),
            action_type=CorporateActionType.SPLIT,
            ratio=None,
        )

    with pytest.raises(ValueError):
        CorporateAction(
            symbol="TCS",
            ex_date=date(2023, 1, 1),
            action_type=CorporateActionType.DIVIDEND,
            amount=-5.0,
        )


def test_corporate_action_rename():
    dates = pd.date_range("2023-01-01", "2023-01-05")
    prices = pd.DataFrame({"AAA": [10.0] * 5, "BBB": [5.0] * 5}, index=dates)
    action = CorporateAction(
        symbol="AAA",
        ex_date=date(2023, 1, 3),
        action_type=CorporateActionType.RENAME,
        new_symbol="AAA_NEW",
    )
    renamed = apply_renames(prices, [action])
    # History keeps the old ticker; ex_date onward uses the new ticker.
    assert renamed.loc["2023-01-01", "AAA"] == 10.0
    assert pd.isna(renamed.loc["2023-01-01", "AAA_NEW"])
    assert pd.isna(renamed.loc["2023-01-04", "AAA"])
    assert renamed.loc["2023-01-04", "AAA_NEW"] == 10.0
    # Raw prices are untouched.
    assert prices.loc["2023-01-04", "AAA"] == 10.0


def test_corporate_action_delisting():
    dates = pd.date_range("2023-01-01", "2023-01-05")
    prices = pd.DataFrame({"BBB": [5.0] * 5}, index=dates)
    action = CorporateAction(
        symbol="BBB",
        ex_date=date(2023, 1, 4),
        action_type=CorporateActionType.DELISTING,
    )
    delisted = apply_delistings(prices, [action])
    assert delisted.loc["2023-01-03", "BBB"] == 5.0
    assert pd.isna(delisted.loc["2023-01-04", "BBB"])
    assert pd.isna(delisted.loc["2023-01-05", "BBB"])


def test_corporate_action_verification_matches_and_mismatches():
    primary = [
        CorporateAction(
            symbol="RELIANCE",
            ex_date=date(2023, 1, 1),
            action_type=CorporateActionType.SPLIT,
            ratio=0.5,
        )
    ]
    agreeing = [
        CorporateAction(
            symbol="RELIANCE",
            ex_date=date(2023, 1, 1),
            action_type=CorporateActionType.SPLIT,
            ratio=0.5,
        )
    ]
    disagreeing = [
        CorporateAction(
            symbol="RELIANCE",
            ex_date=date(2023, 1, 1),
            action_type=CorporateActionType.SPLIT,
            ratio=0.25,
        )
    ]
    assert verify_corporate_actions(primary, agreeing)[0].matched
    assert not verify_corporate_actions(primary, disagreeing)[0].matched
    # A primary event absent from the secondary source is flagged.
    result = verify_corporate_actions(primary, [])[0]
    assert not result.matched and not result.source_b_ok


def test_corporate_action_verification_require_two_sources():
    primary = [
        CorporateAction(
            symbol="RELIANCE",
            ex_date=date(2023, 1, 1),
            action_type=CorporateActionType.DIVIDEND,
            amount=5.0,
        )
    ]
    secondary = [
        CorporateAction(
            symbol="RELIANCE",
            ex_date=date(2023, 1, 1),
            action_type=CorporateActionType.DIVIDEND,
            amount=5.0,
        ),
        CorporateAction(
            symbol="EXTRA",
            ex_date=date(2023, 2, 1),
            action_type=CorporateActionType.DIVIDEND,
            amount=1.0,
        ),
    ]
    results = verify_corporate_actions(primary, secondary, require_two_sources=True)
    assert len(results) == 2
    by_symbol = {r.action.symbol: r for r in results}
    assert by_symbol["RELIANCE"].matched
    assert not by_symbol["EXTRA"].source_a_ok and by_symbol["EXTRA"].source_b_ok


class TestCorporateActionRedTeam:
    """Unknown-action surfacing: never pretend an action was handled."""

    def test_unknown_action_surfaces_when_symbol_absent(self):
        prices = pd.DataFrame(
            {"AAA": [100.0, 101.0, 102.0]},
            index=pd.bdate_range("2026-01-01", periods=3),
        )
        action = CorporateAction(
            symbol="ZZZ",
            ex_date=date(2026, 1, 2),
            action_type=CorporateActionType.SPLIT,
            ratio=0.5,
        )
        report = audit_corporate_action_coverage(prices, [action])
        assert report["clean"] is False
        assert report["findings"][0]["finding"] == "UNKNOWN_CORPORATE_ACTION"
        with pytest.raises(UnknownCorporateActionError):
            apply_corporate_actions(prices, [action], strict=True)
        # Non-strict keeps the legacy silently-skip behaviour for
        # backwards compatibility, but the audit reports it.
        applied = apply_corporate_actions(prices, [action])
        assert applied.equals(prices)

    def test_delisting_not_reflected_flagged(self):
        prices = pd.DataFrame(
            {"AAA": [100.0, 101.0, 102.0, 103.0]},
            index=pd.bdate_range("2026-01-01", periods=4),
        )
        action = CorporateAction(
            symbol="AAA",
            ex_date=date(2026, 1, 3),
            action_type=CorporateActionType.DELISTING,
        )
        report = audit_corporate_action_coverage(prices, [action])
        assert report["clean"] is False
        assert report["findings"][0]["finding"] == "DELISTING_NOT_REFLECTED"
        # after applying the delisting the panel reflects it
        applied = apply_delistings(prices, [action])
        assert applied["AAA"].isna().iloc[2:].all()
        assert audit_corporate_action_coverage(applied, [action])["clean"] is True

    def test_rename_not_reflected_flagged(self):
        prices = pd.DataFrame(
            {"OLD": [100.0, 101.0, 102.0], "OTHER": [50.0, 51.0, 52.0]},
            index=pd.bdate_range("2026-01-01", periods=3),
        )
        action = CorporateAction(
            symbol="OLD",
            ex_date=date(2026, 1, 2),
            action_type=CorporateActionType.RENAME,
            new_symbol="NEW",
        )
        report = audit_corporate_action_coverage(prices, [action])
        assert any(
            finding["finding"] == "RENAME_NOT_REFLECTED"
            for finding in report["findings"]
        )

    def test_outside_panel_is_informational(self):
        prices = pd.DataFrame(
            {"AAA": [100.0, 101.0]},
            index=pd.bdate_range("2026-01-01", periods=2),
        )
        action = CorporateAction(
            symbol="AAA",
            ex_date=date(2027, 6, 1),
            action_type=CorporateActionType.SPLIT,
            ratio=0.5,
        )
        report = audit_corporate_action_coverage(prices, [action])
        assert report["findings"][0]["finding"] == "OUTSIDE_PANEL"

    def test_clean_panel_passes(self):
        prices = pd.DataFrame(
            {"AAA": [200.0, 101.0, 102.0]},
            index=pd.bdate_range("2026-01-01", periods=3),
        )
        # 2:1 split on 2026-01-02: prices before are halved, so 200 -> 100
        action = CorporateAction(
            symbol="AAA",
            ex_date=date(2026, 1, 2),
            action_type=CorporateActionType.SPLIT,
            ratio=0.5,
        )
        applied = apply_corporate_actions(prices, [action])
        assert applied["AAA"].iloc[0] == 100.0
        assert audit_corporate_action_coverage(applied, [action])["clean"] is True
