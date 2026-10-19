from datetime import UTC, datetime, timedelta

import pytest

from app.models import Direction, Regime, Signal
from app.strategies.base import BaseStrategy, StrategyContext
from tests.conftest import make_frame


class StubStrategy(BaseStrategy):
    strategy_id = "stub"
    eligible_regimes = frozenset({Regime.BULLISH_TREND})
    min_candles = 50
    calls = 0

    def _evaluate(self, ctx):
        type(self).calls += 1
        close = float(ctx.candles["close"].iloc[-1])
        return Signal(
            timestamp=ctx.as_of, symbol=ctx.symbol, timeframe=ctx.timeframe, strategy_id=self.strategy_id,
            direction=Direction.LONG, regime=ctx.regime, entry_price=close, stop_price=close - 1,
            take_profit_price=close + 2,
        )


class WrongIdStrategy(StubStrategy):
    strategy_id = "other"

    def _evaluate(self, ctx):
        s = super()._evaluate(ctx)
        return s.model_copy(update={"strategy_id": "imposter"})


def ctx(frame=None, regime=Regime.BULLISH_TREND, as_of=None, **kw):
    frame = frame if frame is not None else make_frame(60)
    as_of = as_of or frame.index[-1].to_pydatetime() + timedelta(hours=1)  # last bar just closed
    return StrategyContext("BTCUSD", "1h", regime, frame, as_of, **kw)


@pytest.fixture(autouse=True)
def _reset():
    StubStrategy.calls = 0


def test_valid_context_reaches_evaluate():
    sig = StubStrategy().generate(ctx())
    assert sig.is_actionable and StubStrategy.calls == 1


@pytest.mark.parametrize("regime", [Regime.SIDEWAYS, Regime.HIGH_VOLATILITY, Regime.UNKNOWN, Regime.BEARISH_TREND])
def test_ineligible_regime_rejected_without_evaluating(regime):
    sig = StubStrategy().generate(ctx(regime=regime))
    assert sig.direction is Direction.NONE and "not eligible" in sig.rejection_reasons[0]
    assert StubStrategy.calls == 0


def test_insufficient_data_rejected():
    sig = StubStrategy().generate(ctx(make_frame(30)))
    assert "insufficient" in sig.rejection_reasons[0] and StubStrategy.calls == 0


def test_incomplete_last_candle_rejected():
    f = make_frame(60)
    sig = StubStrategy().generate(ctx(f, as_of=f.index[-1].to_pydatetime() + timedelta(minutes=30)))
    assert "not yet complete" in sig.rejection_reasons[0] and StubStrategy.calls == 0


def test_stale_data_rejected():
    f = make_frame(60)
    late = f.index[-1].to_pydatetime() + timedelta(hours=5)
    sig = StubStrategy().generate(ctx(f, as_of=late))
    assert "stale" in sig.rejection_reasons[0] and StubStrategy.calls == 0


def test_data_with_gap_rejected():
    f = make_frame(60).drop(make_frame(60).index[20])
    sig = StubStrategy().generate(ctx(f))
    assert "invalid data" in sig.rejection_reasons[0] and StubStrategy.calls == 0


def test_naive_as_of_is_rejected():
    with pytest.raises(ValueError, match="naive"):
        StubStrategy().generate(ctx(as_of=datetime(2024, 1, 3)))


def test_strategy_cannot_return_foreign_signal():
    with pytest.raises(RuntimeError, match="different strategy"):
        WrongIdStrategy().generate(ctx())


def test_abstract_base_cannot_be_instantiated():
    with pytest.raises(TypeError):
        BaseStrategy()  # type: ignore[abstract]
