"""Strategy interface.

``BaseStrategy.generate`` is a template method: shared data/regime guards run first so
no strategy can emit a signal from invalid, stale, incomplete or ineligible inputs.
Concrete strategies implement only ``_evaluate``. A strategy's output is a *proposal*;
the shared risk manager always has the final say.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import ClassVar

import pandas as pd

from app.config import SUPPORTED_TIMEFRAMES
from app.data.frames import FrameError, validate_frame
from app.data.quality import is_stale
from app.models import Direction, Regime, Signal, to_utc


@dataclass(frozen=True)
class StrategyContext:
    """Everything a strategy may look at. Candle frames contain COMPLETED bars only."""

    symbol: str
    timeframe: str
    regime: Regime
    candles: pd.DataFrame
    as_of: datetime
    higher_tf_candles: pd.DataFrame | None = None
    higher_timeframe: str | None = None
    max_staleness_candles: int = 2


class BaseStrategy(ABC):
    strategy_id: ClassVar[str]
    eligible_regimes: ClassVar[frozenset[Regime]]
    min_candles: ClassVar[int]

    def generate(self, ctx: StrategyContext) -> Signal:
        as_of = to_utc(ctx.as_of)

        guard = self._guard(ctx, as_of)
        if guard:
            return self._reject(ctx, as_of, *guard)

        signal = self._evaluate(ctx)
        if (
            signal.strategy_id != self.strategy_id
            or signal.symbol != ctx.symbol
            or signal.timeframe != ctx.timeframe
        ):
            raise RuntimeError(f"{self.strategy_id} returned a signal for a different strategy/symbol/timeframe")
        return signal

    def _guard(self, ctx: StrategyContext, as_of: datetime) -> list[str]:
        if ctx.regime not in self.eligible_regimes:
            return [f"regime {ctx.regime} not eligible for {self.strategy_id}"]
        try:
            validate_frame(ctx.candles, ctx.timeframe)
        except FrameError as exc:
            return [f"invalid data: {exc}"]
        if len(ctx.candles) < self.min_candles:
            return [f"insufficient data: {len(ctx.candles)} < {self.min_candles} candles"]
        step = timedelta(seconds=SUPPORTED_TIMEFRAMES[ctx.timeframe])
        last_open = ctx.candles.index[-1].to_pydatetime()
        last_close = last_open + step
        if last_close > as_of:
            return ["last candle is not yet complete (look-ahead guard)"]
        if is_stale(last_open, ctx.timeframe, as_of, ctx.max_staleness_candles):
            return [f"stale data: last candle closed {as_of - last_close} ago"]
        return []

    @abstractmethod
    def _evaluate(self, ctx: StrategyContext) -> Signal:
        """Return an actionable Signal or ``self._reject(...)``. Inputs are pre-validated."""

    def _reject(self, ctx: StrategyContext, as_of: datetime, *reasons: str) -> Signal:
        return Signal(
            timestamp=as_of,
            symbol=ctx.symbol,
            timeframe=ctx.timeframe,
            strategy_id=self.strategy_id,
            direction=Direction.NONE,
            regime=ctx.regime,
            rejection_reasons=list(reasons),
        )
