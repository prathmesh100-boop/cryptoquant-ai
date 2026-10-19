"""SQLite candle persistence behind a repository interface (PostgreSQL-ready).

Only COMPLETE candles may be stored: ``upsert_candles`` requires ``as_of`` and rejects any bar
that has not closed by then. Upserts are idempotent on (symbol, timeframe, open_time).
"""

from __future__ import annotations

import sqlite3
import threading
from abc import ABC, abstractmethod
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path

import pandas as pd

from app.data.frames import candles_to_frame
from app.models import Candle, to_utc

SCHEMA_VERSION = 1


class StorageError(Exception):
    pass


class CandleRepository(ABC):
    @abstractmethod
    def upsert_candles(self, candles: Sequence[Candle], *, as_of: datetime, source: str = "rest") -> int: ...

    @abstractmethod
    def load_frame(
        self, symbol: str, timeframe: str, start: datetime | None = None, end: datetime | None = None
    ) -> pd.DataFrame:
        """Candles with ``start <= open_time < end`` as a validated-convention frame."""

    @abstractmethod
    def latest_open_time(self, symbol: str, timeframe: str) -> datetime | None: ...

    @abstractmethod
    def count(self, symbol: str, timeframe: str) -> int: ...

    @abstractmethod
    def close(self) -> None: ...


def _sqlite_path_from_url(url: str) -> str:
    prefix = "sqlite:///"
    if not url.startswith(prefix):
        raise StorageError(f"unsupported database url (only sqlite:/// for now): {url.split('://')[0]}://...")
    return url[len(prefix):] or ":memory:"


class SqliteCandleStore(CandleRepository):
    def __init__(self, path: str | Path = ":memory:") -> None:
        path = str(path)
        if path != ":memory:":
            Path(path).expanduser().parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(path, check_same_thread=False)
        if path != ":memory:":
            self._conn.execute("PRAGMA journal_mode=WAL")
        self._init_schema()

    @classmethod
    def from_url(cls, url: str) -> SqliteCandleStore:
        return cls(_sqlite_path_from_url(url))

    def _init_schema(self) -> None:
        with self._lock, self._conn:
            version = self._conn.execute("PRAGMA user_version").fetchone()[0]
            if version > SCHEMA_VERSION:
                raise StorageError(f"database schema v{version} is newer than supported v{SCHEMA_VERSION}")
            self._conn.execute(
                """CREATE TABLE IF NOT EXISTS candles (
                    symbol TEXT NOT NULL, timeframe TEXT NOT NULL, open_time INTEGER NOT NULL,
                    open REAL NOT NULL, high REAL NOT NULL, low REAL NOT NULL, close REAL NOT NULL,
                    volume REAL NOT NULL, source TEXT NOT NULL DEFAULT 'rest',
                    PRIMARY KEY (symbol, timeframe, open_time)) WITHOUT ROWID"""
            )
            self._conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")

    def upsert_candles(self, candles: Sequence[Candle], *, as_of: datetime, source: str = "rest") -> int:
        as_of = to_utc(as_of)
        incomplete = [c.open_time.isoformat() for c in candles if not c.is_complete(as_of)]
        if incomplete:
            raise StorageError(f"refusing to store incomplete candle(s): {incomplete[:3]}")
        rows = [
            (c.symbol, c.timeframe, int(c.open_time.timestamp()), c.open, c.high, c.low, c.close, c.volume, source)
            for c in candles
        ]
        with self._lock, self._conn:
            self._conn.executemany(
                """INSERT INTO candles (symbol, timeframe, open_time, open, high, low, close, volume, source)
                   VALUES (?,?,?,?,?,?,?,?,?)
                   ON CONFLICT(symbol, timeframe, open_time) DO UPDATE SET
                     open=excluded.open, high=excluded.high, low=excluded.low, close=excluded.close,
                     volume=excluded.volume, source=excluded.source""",
                rows,
            )
        return len(rows)

    def load_frame(
        self, symbol: str, timeframe: str, start: datetime | None = None, end: datetime | None = None
    ) -> pd.DataFrame:
        lo = int(to_utc(start).timestamp()) if start else 0
        hi = int(to_utc(end).timestamp()) if end else 2**62
        with self._lock:
            rows = self._conn.execute(
                """SELECT open_time, open, high, low, close, volume FROM candles
                   WHERE symbol=? AND timeframe=? AND open_time>=? AND open_time<?
                   ORDER BY open_time""",
                (symbol, timeframe, lo, hi),
            ).fetchall()
        if not rows:
            return candles_to_frame([])
        df = pd.DataFrame(rows, columns=["open_time", "open", "high", "low", "close", "volume"])
        df.index = pd.DatetimeIndex(pd.to_datetime(df.pop("open_time"), unit="s", utc=True), name="open_time")
        return df.astype(float)

    def latest_open_time(self, symbol: str, timeframe: str) -> datetime | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT MAX(open_time) FROM candles WHERE symbol=? AND timeframe=?", (symbol, timeframe)
            ).fetchone()
        return datetime.fromtimestamp(row[0], tz=UTC) if row and row[0] is not None else None

    def count(self, symbol: str, timeframe: str) -> int:
        with self._lock:
            return self._conn.execute(
                "SELECT COUNT(*) FROM candles WHERE symbol=? AND timeframe=?", (symbol, timeframe)
            ).fetchone()[0]

    def close(self) -> None:
        with self._lock:
            self._conn.close()
