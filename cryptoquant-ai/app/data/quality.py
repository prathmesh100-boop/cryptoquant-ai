"""Data-quality checks and look-ahead-safe resampling for candle frames."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

import pandas as pd

from app.config import SUPPORTED_TIMEFRAMES
from app.data.frames import FrameError, timeframe_delta
from app.models import Candle, to_utc

_EPOCH = pd.Timestamp(0, tz="UTC")


@dataclass(frozen=True)
class Gap:
    """A run of missing bars. ``first_missing``/``last_missing`` are bar open times."""

    first_missing: datetime
    last_missing: datetime
    missing_bars: int


def find_gaps(frame: pd.DataFrame, timeframe: str) -> list[Gap]:
    """Report runs of missing bars between the first and last candle. Expects a sorted, unique index."""
    if len(frame) < 2:
        return []
    step = pd.Timedelta(timeframe_delta(timeframe))
    idx = frame.index
    diffs = idx[1:] - idx[:-1]
    gaps: list[Gap] = []
    for i in (diffs > step).nonzero()[0]:
        missing = int(diffs[i] // step) - 1
        first = idx[i] + step
        gaps.append(Gap(first.to_pydatetime(), (idx[i + 1] - step).to_pydatetime(), missing))
    return gaps


def last_close_time(frame: pd.DataFrame, timeframe: str) -> datetime:
    return (frame.index[-1] + pd.Timedelta(timeframe_delta(timeframe))).to_pydatetime()


def data_age(last_open_time: datetime, timeframe: str, now: datetime) -> timedelta:
    """Time since the latest candle *closed*. Negative means that candle has not closed yet."""
    return to_utc(now) - (to_utc(last_open_time) + timeframe_delta(timeframe))


def is_stale(last_open_time: datetime, timeframe: str, now: datetime, max_candles: int = 2) -> bool:
    return data_age(last_open_time, timeframe, now) > timeframe_delta(timeframe) * max_candles


def resample(frame: pd.DataFrame, src_timeframe: str, dst_timeframe: str) -> pd.DataFrame:
    """Aggregate completed ``src`` bars into ``dst`` bars on the UTC epoch grid.

    A destination bar is emitted only when *every* constituent source bar is present, so the
    result never contains a partial bar and never uses data beyond the last source bar
    (appending future bars cannot change earlier output rows).
    """
    src, dst = SUPPORTED_TIMEFRAMES[src_timeframe], SUPPORTED_TIMEFRAMES[dst_timeframe]
    if dst <= src or dst % src != 0:
        raise FrameError(f"cannot resample {src_timeframe} -> {dst_timeframe}")
    cols = ["open", "high", "low", "close", "volume"]
    if frame.empty:
        return frame[cols].copy()
    ratio = dst // src
    epoch_s = ((frame.index - _EPOCH) // pd.Timedelta(seconds=1)).to_numpy()
    bucket = (epoch_s // dst) * dst
    grouped = frame[cols].groupby(bucket)
    out = grouped.agg({"open": "first", "high": "max", "low": "min", "close": "last", "volume": "sum"})
    out = out[grouped.size() == ratio]
    out.index = pd.DatetimeIndex(pd.to_datetime(out.index.to_numpy(), unit="s", utc=True), name="open_time")
    return out


def diff_candles(a: "Candle", b: "Candle", rel_tol: float = 1e-9) -> dict[str, tuple[float, float]]:
    """Fields where two candles for the same bar disagree (e.g. WebSocket vs REST). Empty dict = identical."""
    if (a.symbol, a.timeframe, a.open_time) != (b.symbol, b.timeframe, b.open_time):
        raise ValueError("candles are for different bars")
    out: dict[str, tuple[float, float]] = {}
    for f in ("open", "high", "low", "close", "volume"):
        x, y = getattr(a, f), getattr(b, f)
        if abs(x - y) > rel_tol * max(abs(x), abs(y), 1.0):
            out[f] = (x, y)
    return out
