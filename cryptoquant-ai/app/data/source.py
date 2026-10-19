"""Historical candle source interface and a CSV implementation."""

from __future__ import annotations

from abc import ABC, abstractmethod
from datetime import datetime
from pathlib import Path

import pandas as pd

from app.models import Candle, to_utc


class DataSourceError(Exception):
    """Base error for market-data retrieval problems."""


class RateLimitedError(DataSourceError):
    """The exchange kept returning HTTP 429 after bounded retries."""


class MalformedResponseError(DataSourceError):
    """The response violated the documented schema or a data invariant. Never silently repaired."""


class HistoricalCandleSource(ABC):
    @abstractmethod
    def fetch_candles(self, symbol: str, timeframe: str, start: datetime, end: datetime) -> list[Candle]:
        """Return COMPLETE candles with ``start <= open_time < end``, sorted ascending and unique."""


class CsvCandleSource(HistoricalCandleSource):
    """Offline source for CSV files with columns ``open_time,open,high,low,close,volume``.

    ``open_time`` is either epoch seconds or an ISO-8601 string with a timezone (naive is rejected).
    """

    def __init__(self, path: str | Path) -> None:
        self._path = Path(path)

    def fetch_candles(self, symbol: str, timeframe: str, start: datetime, end: datetime) -> list[Candle]:
        try:
            df = pd.read_csv(self._path)
        except (OSError, pd.errors.ParserError) as exc:
            raise DataSourceError(f"cannot read {self._path}: {exc}") from exc
        required = {"open_time", "open", "high", "low", "close", "volume"}
        if not required <= set(df.columns):
            raise MalformedResponseError(f"CSV missing columns: {sorted(required - set(df.columns))}")
        start, end = to_utc(start), to_utc(end)
        out: dict[datetime, Candle] = {}
        for row in df.itertuples(index=False):
            ts = row.open_time
            try:
                if isinstance(ts, str) and not ts.strip().lstrip("-").isdigit():
                    t = to_utc(datetime.fromisoformat(ts.replace("Z", "+00:00")))
                else:
                    t = pd.Timestamp(int(ts), unit="s", tz="UTC").to_pydatetime()
                candle = Candle(symbol=symbol, timeframe=timeframe, open_time=t, open=row.open, high=row.high,
                                low=row.low, close=row.close, volume=row.volume)
            except (ValueError, TypeError) as exc:
                raise MalformedResponseError(f"bad CSV row {tuple(row)}: {exc}") from exc
            if start <= candle.open_time < end:
                out[candle.open_time] = candle
        return [out[k] for k in sorted(out)]
