from datetime import UTC, datetime, timedelta

import pandas as pd
import pytest

from app.data.frames import FrameError, floor_time, is_aligned
from app.data.quality import data_age, diff_candles, find_gaps, is_stale, resample
from tests.conftest import T0, make_frame


def test_no_gaps_in_clean_frame():
    assert find_gaps(make_frame(), "1h") == []


def test_single_and_multiple_gaps_reported():
    f = make_frame(30)
    f = f.drop(f.index[[5, 6, 7, 20]])
    gaps = find_gaps(f, "1h")
    assert [(g.first_missing, g.last_missing, g.missing_bars) for g in gaps] == [
        (T0 + timedelta(hours=5), T0 + timedelta(hours=7), 3),
        (T0 + timedelta(hours=20), T0 + timedelta(hours=20), 1),
    ]


def test_gaps_on_tiny_frames():
    assert find_gaps(make_frame(1), "1h") == [] and find_gaps(make_frame(2), "1h") == []


def test_staleness_boundaries():
    last_open = T0
    close = T0 + timedelta(hours=1)
    assert data_age(last_open, "1h", close) == timedelta(0)
    assert not is_stale(last_open, "1h", close + timedelta(hours=2), max_candles=2)
    assert is_stale(last_open, "1h", close + timedelta(hours=2, seconds=1), max_candles=2)
    assert data_age(last_open, "1h", T0 + timedelta(minutes=30)) < timedelta(0)  # bar not closed yet


def test_floor_and_alignment():
    t = datetime(2024, 1, 1, 10, 37, 12, tzinfo=UTC)
    assert floor_time(t, "1h") == datetime(2024, 1, 1, 10, tzinfo=UTC)
    assert floor_time(t, "4h") == datetime(2024, 1, 1, 8, tzinfo=UTC)
    assert floor_time(t, "15m") == datetime(2024, 1, 1, 10, 30, tzinfo=UTC)
    assert is_aligned(datetime(2024, 1, 1, 8, tzinfo=UTC), "4h") and not is_aligned(t, "1h")


def test_resample_aggregates_correctly_and_drops_partial_bucket():
    f = make_frame(10)  # 2 full 4h buckets + 2 leftover 1h bars
    out = resample(f, "1h", "4h")
    assert len(out) == 2
    first = f.iloc[:4]
    row = out.iloc[0]
    assert row["open"] == first["open"].iloc[0] and row["close"] == first["close"].iloc[-1]
    assert row["high"] == first["high"].max() and row["low"] == first["low"].min()
    assert row["volume"] == first["volume"].sum()
    assert out.index[0] == f.index[0] and str(out.index.tz) == "UTC"


def test_resample_requires_complete_buckets_when_bars_missing():
    f = make_frame(8).drop(make_frame(8).index[2])
    out = resample(f, "1h", "4h")
    assert len(out) == 1 and out.index[0] == T0 + timedelta(hours=4)


def test_resample_has_no_lookahead():
    """Appending future bars must never change already-emitted rows."""
    f = make_frame(40)
    full = resample(f, "1h", "4h")
    for k in range(1, 41):
        part = resample(f.iloc[:k], "1h", "4h")
        pd.testing.assert_frame_equal(part, full.iloc[: len(part)])


@pytest.mark.parametrize("src,dst", [("1h", "1h"), ("4h", "1h"), ("1h", "15m"), ("1h", "30m")])
def test_resample_rejects_invalid_ratio(src, dst):
    with pytest.raises(FrameError):
        resample(make_frame(8), src, dst)


def test_resample_empty():
    assert resample(make_frame(8).iloc[0:0], "1h", "4h").empty


def test_diff_candles():
    from tests.conftest import make_candles
    a, b = make_candles(2)[0], make_candles(2)[0]
    assert diff_candles(a, b) == {}
    assert diff_candles(a, b.model_copy(update={"close": 100.5, "volume": 99.0})) == {
        "close": (a.close, 100.5), "volume": (a.volume, 99.0)}
    with pytest.raises(ValueError):
        diff_candles(a, make_candles(2)[1])
