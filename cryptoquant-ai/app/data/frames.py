"""OHLCV DataFrame convention.

A candle frame has a tz-aware UTC ``DatetimeIndex`` of bar *open* times (strictly
increasing, unique) and float columns open/high/low/close/volume.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime

import pandas as pd

from app.config import SUPPORTED_TIMEFRAMES
from app.models import Candle, to_utc

OHLCV_COLUMNS = ["open", "high", "low", "close", "volume"]


class FrameError(ValueError):
    """Raised when a candle frame violates the convention."""


def candles_to_frame(candles: Sequence[Candle]) -> pd.DataFrame:
    if not candles:
        return pd.DataFrame(columns=OHLCV_COLUMNS, index=pd.DatetimeIndex([], tz="UTC", name="open_time"))
    frame = pd.DataFrame(
        {c: [getattr(k, c) for k in candles] for c in OHLCV_COLUMNS},
        index=pd.DatetimeIndex([k.open_time for k in candles], name="open_time"),
    )
    return frame


def validate_frame(frame: pd.DataFrame, timeframe: str) -> None:
    """Raise ``FrameError`` on a malformed frame. Gaps are reported, not silently filled."""
    if timeframe not in SUPPORTED_TIMEFRAMES:
        raise FrameError(f"unsupported timeframe {timeframe}")
    missing = [c for c in OHLCV_COLUMNS if c not in frame.columns]
    if missing:
        raise FrameError(f"missing columns: {missing}")
    idx = frame.index
    if not isinstance(idx, pd.DatetimeIndex) or idx.tz is None or str(idx.tz) != "UTC":
        raise FrameError("index must be a UTC-aware DatetimeIndex")
    if not idx.is_monotonic_increasing:
        raise FrameError("index must be sorted ascending")
    if idx.has_duplicates:
        raise FrameError("duplicate candle timestamps")
    if frame[OHLCV_COLUMNS].isna().any().any():
        raise FrameError("NaN values present")
    step = pd.Timedelta(seconds=SUPPORTED_TIMEFRAMES[timeframe])
    # Absolute alignment: bars must open on the UTC epoch grid for this timeframe.
    if len(idx) and ((idx - pd.Timestamp(0, tz="UTC")) % step != pd.Timedelta(0)).any():
        raise FrameError("candle timestamps not aligned to timeframe")
    if len(idx) > 1:
        diffs = idx[1:] - idx[:-1]
        if (diffs > step).any():
            raise FrameError(f"missing candles detected ({int((diffs > step).sum())} gap(s))")


def last_complete_index(frame: pd.DataFrame, timeframe: str, as_of: datetime) -> int:
    """Number of leading rows whose bar has fully closed by ``as_of``."""
    step = pd.Timedelta(seconds=SUPPORTED_TIMEFRAMES[timeframe])
    closes = frame.index + step
    return int((closes <= pd.Timestamp(to_utc(as_of))).sum())
