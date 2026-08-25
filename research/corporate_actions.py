"""Corporate action processing for the research environment.

This module applies splits, bonuses, and dividends to historical prices without
mutating the immutable raw price history.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from enum import Enum
from typing import Any, Sequence

import pandas as pd
from pydantic import BaseModel, ConfigDict, field_validator


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
    new_symbol: str | None = None

    @field_validator("ratio")
    @classmethod
    def _valid_ratio(cls, v: float | None, info: Any) -> float | None:
        if info.data.get("action_type") in (
            CorporateActionType.SPLIT,
            CorporateActionType.BONUS,
        ):
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
    prices: pd.DataFrame, actions: list[CorporateAction]
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
            adjusted.loc[mask, action.symbol] = adjusted.loc[mask, action.symbol].clip(
                lower=0.01
            )

    return adjusted


def apply_renames(
    prices: pd.DataFrame,
    actions: Sequence[CorporateAction],
) -> pd.DataFrame:
    """Apply RENAME actions to a wide price panel.

    A rename re-labels the symbol column from the ``ex_date`` onward
    (dates on/after ``ex_date`` use the new name; dates strictly before
    keep the old name). The returned frame may therefore carry a
    ``symbol -> new_symbol`` pair of columns. The input frame is not
    mutated.
    """
    if not isinstance(prices, pd.DataFrame):
        raise TypeError("prices must be a pandas DataFrame")
    renamed = prices.copy()
    for action in actions:
        if action.action_type != CorporateActionType.RENAME:
            continue
        new_symbol = action.new_symbol
        if not new_symbol:
            raise ValueError(f"RENAME action for {action.symbol} needs a new symbol")
        if action.symbol not in renamed.columns:
            continue
        if new_symbol in renamed.columns:
            raise ValueError(
                f"RENAME target {new_symbol} already present in price panel"
            )
        ex_ts = pd.Timestamp(action.ex_date)
        old_column = renamed[action.symbol]
        if ex_ts in renamed.index:
            # Split the series at the ex_date: past keeps the old name,
            # present and future use the new name.
            before = renamed.index < ex_ts
            renamed[new_symbol] = old_column.where(~before)
            renamed[action.symbol] = old_column.where(before)
        else:
            # ex_date outside the panel: apply wholesale.
            renamed[new_symbol] = old_column
            renamed = renamed.drop(columns=[action.symbol])
    return renamed


def apply_delistings(
    prices: pd.DataFrame,
    actions: Sequence[CorporateAction],
) -> pd.DataFrame:
    """Apply DELISTING actions by removing the symbol from the ex_date.

    Dates on/after the delisting ``ex_date`` become missing for that symbol
    (the delisted name is dropped from the tradable universe), while the
    pre-delisting history is preserved so survivorship bias stays visible.
    The input frame is not mutated.
    """
    if not isinstance(prices, pd.DataFrame):
        raise TypeError("prices must be a pandas DataFrame")
    delisted = prices.copy()
    for action in actions:
        if action.action_type != CorporateActionType.DELISTING:
            continue
        if action.symbol not in delisted.columns:
            continue
        delisted[action.symbol] = delisted[action.symbol].where(
            delisted.index < pd.Timestamp(action.ex_date)
        )
    return delisted


@dataclass(frozen=True, slots=True)
class VerificationResult:
    """Outcome of cross-checking one corporate action against a second source."""

    action: CorporateAction
    matched: bool
    source_a_ok: bool
    source_b_ok: bool
    detail: str = ""

    def to_dict(self) -> dict[str, Any]:
        """Return a serializable verification result."""
        return {
            "symbol": self.action.symbol,
            "action_type": self.action.action_type.value,
            "ex_date": self.action.ex_date.isoformat(),
            "matched": self.matched,
            "source_a_ok": self.source_a_ok,
            "source_b_ok": self.source_b_ok,
            "detail": self.detail,
        }


def _action_key(action: CorporateAction) -> tuple[str, str, str]:
    return (
        action.symbol.strip().upper(),
        action.action_type.value,
        action.ex_date.isoformat(),
    )


def _same_event(a: CorporateAction, b: CorporateAction) -> bool:
    """Compare two records of the same action type for material equality."""
    if a.action_type != b.action_type:
        return False
    if a.action_type in (CorporateActionType.SPLIT, CorporateActionType.BONUS):
        return (
            a.ratio is not None
            and b.ratio is not None
            and abs(a.ratio - b.ratio) <= 1e-12
        )
    if a.action_type == CorporateActionType.DIVIDEND:
        return (
            a.amount is not None
            and b.amount is not None
            and abs(a.amount - b.amount) <= 1e-9
        )
    if a.action_type == CorporateActionType.RENAME:
        return a.new_symbol == b.new_symbol
    return True


def verify_corporate_actions(
    primary: Sequence[CorporateAction],
    secondary: Sequence[CorporateAction],
    *,
    require_two_sources: bool = False,
) -> list[VerificationResult]:
    """Cross-check corporate actions against a second source.

    Every primary action is compared to the secondary source by
    ``(symbol, type, ex_date)``. A matched event also confirms the
    magnitude/ratio agrees. Unmatched events are reported (the secondary
    source may be incomplete). When ``require_two_sources`` is set,
    ``source_a_ok``/``source_b_ok`` reflect whether each source recorded the
    event and the record is only ``matched`` when both agree.
    """
    secondary_by_key: dict[tuple[str, str, str], CorporateAction] = {}
    for action in secondary:
        secondary_by_key.setdefault(_action_key(action), action)

    results: list[VerificationResult] = []
    for action in primary:
        match = secondary_by_key.get(_action_key(action))
        source_b_ok = match is not None
        matched = source_b_ok and _same_event(action, match)
        detail = ""
        if not source_b_ok:
            detail = "event not present in secondary source"
        elif not matched:
            detail = "event present in both sources but details differ"
        results.append(
            VerificationResult(
                action=action,
                matched=matched,
                source_a_ok=True,
                source_b_ok=source_b_ok,
                detail=detail,
            )
        )
        if source_b_ok:
            del secondary_by_key[_action_key(action)]
    if require_two_sources:
        for leftover in secondary_by_key.values():
            results.append(
                VerificationResult(
                    action=leftover,
                    matched=False,
                    source_a_ok=False,
                    source_b_ok=True,
                    detail="event only present in secondary source",
                )
            )
    return results
