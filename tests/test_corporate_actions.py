import pytest
import pandas as pd
from datetime import date

from research.corporate_actions import (
    CorporateAction, CorporateActionType, apply_corporate_actions
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
