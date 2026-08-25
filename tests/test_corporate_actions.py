from datetime import date

import pandas as pd
import pytest

from research.corporate_actions import (
    CorporateAction,
    CorporateActionType,
    apply_corporate_actions,
    apply_delistings,
    apply_renames,
    verify_corporate_actions,
)


def test_corporate_action_split():
    dates = pd.date_range("2023-01-01", "2023-01-05")
    prices = pd.DataFrame({
        "RELIANCE": [100.0, 100.0, 100.0, 50.0, 50.0]
    }, index=dates)
    
    action = CorporateAction(
        symbol="RELIANCE",
        ex_date=date(2023, 1, 4),
        action_type=CorporateActionType.SPLIT,
        ratio=0.5
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
    prices = pd.DataFrame({
        "INFY": [150.0, 150.0, 150.0, 140.0, 140.0]
    }, index=dates)
    
    action = CorporateAction(
        symbol="INFY",
        ex_date=date(2023, 1, 4),
        action_type=CorporateActionType.DIVIDEND,
        amount=10.0
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
            ratio=None
        )
        
    with pytest.raises(ValueError):
        CorporateAction(
            symbol="TCS",
            ex_date=date(2023, 1, 1),
            action_type=CorporateActionType.DIVIDEND,
            amount=-5.0
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
            symbol="RELIANCE", ex_date=date(2023, 1, 1),
            action_type=CorporateActionType.SPLIT, ratio=0.5,
        )
    ]
    agreeing = [
        CorporateAction(
            symbol="RELIANCE", ex_date=date(2023, 1, 1),
            action_type=CorporateActionType.SPLIT, ratio=0.5,
        )
    ]
    disagreeing = [
        CorporateAction(
            symbol="RELIANCE", ex_date=date(2023, 1, 1),
            action_type=CorporateActionType.SPLIT, ratio=0.25,
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
            symbol="RELIANCE", ex_date=date(2023, 1, 1),
            action_type=CorporateActionType.DIVIDEND, amount=5.0,
        )
    ]
    secondary = [
        CorporateAction(
            symbol="RELIANCE", ex_date=date(2023, 1, 1),
            action_type=CorporateActionType.DIVIDEND, amount=5.0,
        ),
        CorporateAction(
            symbol="EXTRA", ex_date=date(2023, 2, 1),
            action_type=CorporateActionType.DIVIDEND, amount=1.0,
        ),
    ]
    results = verify_corporate_actions(primary, secondary, require_two_sources=True)
    assert len(results) == 2
    by_symbol = {r.action.symbol: r for r in results}
    assert by_symbol["RELIANCE"].matched
    assert not by_symbol["EXTRA"].source_a_ok and by_symbol["EXTRA"].source_b_ok
