"""Corporate action processing for the research environment.

This module applies splits, bonuses, and dividends to historical prices without
mutating the immutable raw price history.
"""

from __future__ import annotations

from datetime import date
from enum import Enum
from typing import Any, Mapping

import pandas as pd
from pydantic import BaseModel, ConfigDict, Field, field_validator

class CorporateActionType(str, Enum):
    SPLIT = "SPLIT"
    BONUS = "BONUS"
    DIVIDEND = "DIVIDEND"
    RENAME = "RENAME"
    DELISTING = "DELISTING"

class CorporateAction(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    
    symbol: str
    ex_date: date
    action_type: CorporateActionType
    ratio: float | None = None
    amount: float | None = None

    @field_validator("ratio")
    @classmethod
    def _valid_ratio(cls, v: float | None, info: Any) -> float | None:
        if info.data.get("action_type") in (CorporateActionType.SPLIT, CorporateActionType.BONUS):
            if v is None or v <= 0:
                raise ValueError("ratio must be positive for split/bonus")
        return v

    @field_validator("amount")
    @classmethod
    def _valid_amount(cls, v: float | None, info: Any) -> float | None:
        if info.data.get("action_type") == CorporateActionType.DIVIDEND:
            if v is None or v <= 0:
                raise ValueError("amount must be positive for dividend")
        return v


def apply_corporate_actions(
    prices: pd.DataFrame,
    actions: list[CorporateAction]
) -> pd.DataFrame:
    """Apply corporate actions to produce an adjusted price series.
    
    The original prices DataFrame remains unmodified (preserves raw history).
    Returns a new DataFrame with adjusted prices.
    
    Prices should be indexed by date and columns should be symbols.
    """
    adjusted = prices.copy().astype(float)
    
    # Sort actions by ex_date descending to apply from newest to oldest
    sorted_actions = sorted(actions, key=lambda a: a.ex_date, reverse=True)
    
    for action in sorted_actions:
        if action.symbol not in adjusted.columns:
            continue
            
        # Mask for dates strictly before the ex_date
        # Adjusted prices affect historical data prior to the corporate action
        mask = adjusted.index < pd.Timestamp(action.ex_date)
        
        if action.action_type in (CorporateActionType.SPLIT, CorporateActionType.BONUS):
            # Price decreases by ratio, so multiply historical prices by ratio
            # e.g., 2:1 split -> ratio = 0.5. Old price 100 becomes 50.
            adjusted.loc[mask, action.symbol] *= action.ratio
            
        elif action.action_type == CorporateActionType.DIVIDEND:
            # Absolute dividend amount subtracted from historical prices
            adjusted.loc[mask, action.symbol] -= action.amount
            # Floor at 0.01 to prevent negative prices
            adjusted.loc[mask, action.symbol] = adjusted.loc[mask, action.symbol].clip(lower=0.01)
            
    return adjusted
