from datetime import UTC, datetime, timedelta, timezone

import pytest
from pydantic import ValidationError

from app.models import Candle, DecisionAction, Direction, Regime, Signal, TradeDecision

T0 = datetime(2024, 1, 1, tzinfo=UTC)


def candle(**kw):
    base = dict(symbol="BTCUSD", timeframe="1h", open_time=T0, open=100, high=105, low=95, close=102, volume=3)
    base.update(kw)
    return Candle(**base)


def signal(**kw):
    base = dict(
        timestamp=T0, symbol="BTCUSD", timeframe="1h", strategy_id="x", direction=Direction.LONG,
        regime=Regime.BULLISH_TREND, entry_price=100.0, stop_price=98.0, take_profit_price=106.0,
    )
    base.update(kw)
    return Signal(**base)


def test_candle_normalizes_to_utc():
    ist = timezone(timedelta(hours=5, minutes=30))
    c = candle(open_time=datetime(2024, 1, 1, 5, 30, tzinfo=ist))
    assert c.open_time == T0 and c.open_time.utcoffset() == timedelta(0)


def test_candle_rejects_naive_datetime():
    with pytest.raises(ValidationError, match="naive"):
        candle(open_time=datetime(2024, 1, 1))


@pytest.mark.parametrize(
    "kw", [dict(high=99), dict(low=101), dict(open=-1), dict(volume=-1), dict(close=float("nan")), dict(timeframe="9m")]
)
def test_candle_rejects_invalid(kw):
    with pytest.raises(ValidationError):
        candle(**kw)


def test_candle_completeness():
    c = candle()
    assert c.close_time == T0 + timedelta(hours=1)
    assert not c.is_complete(T0 + timedelta(minutes=59, seconds=59))
    assert c.is_complete(T0 + timedelta(hours=1))


def test_candle_is_immutable():
    with pytest.raises(ValidationError):
        candle().close = 1  # type: ignore[misc]


def test_long_signal_reward_risk():
    assert signal().gross_reward_risk == pytest.approx(3.0)


def test_short_signal_reward_risk():
    s = signal(direction=Direction.SHORT, entry_price=100.0, stop_price=102.0, take_profit_price=96.0)
    assert s.gross_reward_risk == pytest.approx(2.0)


@pytest.mark.parametrize(
    "kw",
    [
        dict(stop_price=None),  # mandatory protective stop
        dict(stop_price=101.0),  # long stop above entry
        dict(take_profit_price=99.0),  # long TP below entry
        dict(rejection_reasons=["x"]),  # actionable with rejection
    ],
)
def test_invalid_actionable_signals(kw):
    with pytest.raises(ValidationError):
        signal(**kw)


def test_none_signal_requires_reason_and_no_prices():
    with pytest.raises(ValidationError, match="rejection reason"):
        signal(direction=Direction.NONE, entry_price=None, stop_price=None, take_profit_price=None)
    with pytest.raises(ValidationError, match="prices"):
        signal(direction=Direction.NONE, rejection_reasons=["no setup"])
    ok = signal(direction=Direction.NONE, entry_price=None, stop_price=None, take_profit_price=None,
                rejection_reasons=["no setup"])
    assert not ok.is_actionable and ok.gross_reward_risk is None


def test_trade_decision_rules():
    with pytest.raises(ValidationError):
        TradeDecision(timestamp=T0, symbol="BTCUSD", action=DecisionAction.NO_TRADE)  # needs a reason
    with pytest.raises(ValidationError):
        TradeDecision(timestamp=T0, symbol="BTCUSD", action=DecisionAction.TRADE)  # needs a signal
    d = TradeDecision(timestamp=T0, symbol="BTCUSD", action=DecisionAction.TRADE, signal=signal())
    assert d.signal is not None
