from datetime import UTC, datetime

import pandas as pd
import pytest

from app.data.frames import FrameError, candles_to_frame, last_complete_index, validate_frame
from app.models import Candle
from tests.conftest import make_frame


def test_valid_frame_passes():
    validate_frame(make_frame(), "1h")


def test_candles_to_frame_roundtrip():
    c = [Candle(symbol="BTCUSD", timeframe="1h", open_time=datetime(2024, 1, 1, h, tzinfo=UTC),
                open=1, high=2, low=1, close=2, volume=1) for h in range(3)]
    f = candles_to_frame(c)
    assert len(f) == 3 and str(f.index.tz) == "UTC"
    validate_frame(f, "1h")


@pytest.mark.parametrize(
    "mutate,match",
    [
        (lambda f: f.drop(columns=["volume"]), "missing columns"),
        (lambda f: f.tz_localize(None), "UTC"),
        (lambda f: f.iloc[::-1], "sorted"),
        (lambda f: pd.concat([f, f.iloc[[-1]]]), "duplicate"),
        (lambda f: f.drop(f.index[10]), "missing candles"),
        (lambda f: f.assign(close=f["close"].where(f.index != f.index[5])), "NaN"),
    ],
)
def test_invalid_frames_detected(mutate, match):
    f = make_frame()
    with pytest.raises(FrameError, match=match):
        validate_frame(mutate(f), "1h")


def test_misaligned_timestamps_detected():
    f = make_frame()
    f.index = f.index + pd.Timedelta(minutes=7)
    with pytest.raises(FrameError, match="aligned"):
        validate_frame(f, "1h")


def test_last_complete_index():
    f = make_frame(n=5)
    # bar 4 opens 04:00 and closes 05:00
    assert last_complete_index(f, "1h", datetime(2024, 1, 1, 4, 59, tzinfo=UTC)) == 4
    assert last_complete_index(f, "1h", datetime(2024, 1, 1, 5, 0, tzinfo=UTC)) == 5
