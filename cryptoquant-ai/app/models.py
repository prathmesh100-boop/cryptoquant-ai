"""Core domain models shared across modules. All models are immutable and validated."""

from __future__ import annotations

import math
from datetime import UTC, datetime, timedelta
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field, computed_field, field_validator, model_validator

from app.config import SUPPORTED_TIMEFRAMES


def to_utc(dt: datetime) -> datetime:
    """Normalize to timezone-aware UTC. Naive datetimes are rejected, never guessed."""
    if dt.tzinfo is None:
        raise ValueError("naive datetime not allowed; timestamps must be timezone-aware")
    return dt.astimezone(UTC)


class Direction(StrEnum):
    LONG = "LONG"
    SHORT = "SHORT"
    NONE = "NONE"


class Regime(StrEnum):
    BULLISH_TREND = "BULLISH_TREND"
    BEARISH_TREND = "BEARISH_TREND"
    SIDEWAYS = "SIDEWAYS"
    HIGH_VOLATILITY = "HIGH_VOLATILITY"
    UNKNOWN = "UNKNOWN"


class DecisionAction(StrEnum):
    TRADE = "TRADE"
    NO_TRADE = "NO_TRADE"


class _Frozen(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class Candle(_Frozen):
    """A single OHLCV bar. ``open_time`` is the bar's start, in UTC."""

    symbol: str
    timeframe: str
    open_time: datetime
    open: float
    high: float
    low: float
    close: float
    volume: float = Field(ge=0)

    @field_validator("open_time")
    @classmethod
    def _utc(cls, v: datetime) -> datetime:
        return to_utc(v)

    @field_validator("timeframe")
    @classmethod
    def _tf(cls, v: str) -> str:
        if v not in SUPPORTED_TIMEFRAMES:
            raise ValueError(f"unsupported timeframe {v}")
        return v

    @model_validator(mode="after")
    def _ohlc_consistent(self) -> Candle:
        vals = (self.open, self.high, self.low, self.close, self.volume)
        if not all(math.isfinite(x) for x in vals):
            raise ValueError("non-finite value in candle")
        if min(self.open, self.high, self.low, self.close) <= 0:
            raise ValueError("prices must be positive")
        if self.high < max(self.open, self.close, self.low) or self.low > min(self.open, self.close, self.high):
            raise ValueError("inconsistent OHLC: high/low do not bound open/close")
        return self

    @property
    def close_time(self) -> datetime:
        """Instant at which the bar completes (exclusive end)."""
        return self.open_time + timedelta(seconds=SUPPORTED_TIMEFRAMES[self.timeframe])

    def is_complete(self, as_of: datetime) -> bool:
        return self.close_time <= to_utc(as_of)


class Signal(_Frozen):
    """Structured strategy output. Either actionable (LONG/SHORT) or a documented rejection (NONE)."""

    timestamp: datetime
    symbol: str
    timeframe: str
    strategy_id: str
    direction: Direction
    regime: Regime
    entry_conditions: list[str] = Field(default_factory=list)
    entry_price: float | None = Field(None, gt=0)
    stop_price: float | None = Field(None, gt=0)
    take_profit_price: float | None = Field(None, gt=0)
    exit_conditions: list[str] = Field(default_factory=list)
    rejection_reasons: list[str] = Field(default_factory=list)

    @field_validator("timestamp")
    @classmethod
    def _utc(cls, v: datetime) -> datetime:
        return to_utc(v)

    @model_validator(mode="after")
    def _consistent(self) -> Signal:
        if self.direction is Direction.NONE:
            if not self.rejection_reasons:
                raise ValueError("a NONE signal must state at least one rejection reason")
            if self.entry_price or self.stop_price or self.take_profit_price:
                raise ValueError("a NONE signal must not carry prices")
            return self
        if self.rejection_reasons:
            raise ValueError("an actionable signal must not carry rejection reasons")
        if self.entry_price is None or self.stop_price is None:
            raise ValueError("actionable signals require entry_price and stop_price (mandatory protective stop)")
        long = self.direction is Direction.LONG
        if (long and self.stop_price >= self.entry_price) or (not long and self.stop_price <= self.entry_price):
            raise ValueError("stop must be on the losing side of entry")
        tp = self.take_profit_price
        if tp is not None and ((long and tp <= self.entry_price) or (not long and tp >= self.entry_price)):
            raise ValueError("take-profit must be on the winning side of entry")
        return self

    @property
    def is_actionable(self) -> bool:
        return self.direction is not Direction.NONE

    @computed_field  # type: ignore[prop-decorator]
    @property
    def gross_reward_risk(self) -> float | None:
        """Gross R:R before fees/slippage; the selector computes the net figure."""
        if not self.is_actionable or self.take_profit_price is None:
            return None
        assert self.entry_price is not None and self.stop_price is not None
        return abs(self.take_profit_price - self.entry_price) / abs(self.entry_price - self.stop_price)


class TradeDecision(_Frozen):
    """Selector output. Every decision records its reasoning."""

    timestamp: datetime
    symbol: str
    action: DecisionAction
    signal: Signal | None = None
    reasons: list[str] = Field(default_factory=list)

    @field_validator("timestamp")
    @classmethod
    def _utc(cls, v: datetime) -> datetime:
        return to_utc(v)

    @model_validator(mode="after")
    def _consistent(self) -> TradeDecision:
        if self.action is DecisionAction.TRADE and (self.signal is None or not self.signal.is_actionable):
            raise ValueError("TRADE requires an actionable signal")
        if self.action is DecisionAction.NO_TRADE and not self.reasons:
            raise ValueError("NO_TRADE requires at least one reason")
        return self
