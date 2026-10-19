"""Delta Exchange India public REST client (historical candles only; no authentication).

Verified against https://docs.delta.exchange and Delta India help pages (Oct 2026):
  * GET /v2/history/candles?resolution=&symbol=&start=&end=  (start/end = Unix seconds)
  * up to 2000 candles per request; rows are {time, open, high, low, close, volume}
  * HTTP 429 carries ``X-RATE-LIMIT-RESET`` (milliseconds); a User-Agent header is mandatory
  * envelope: {"success": true, "result": [...]}

NOT verified (no network access to the exchange from the build sandbox):
  * whether ``end`` is inclusive, and whether ``time`` is the bar OPEN time. The client defends
    against both (dedupe, range filter, strict grid-alignment check) but live behaviour must be
    confirmed with ``scripts/smoke_test_delta.py``.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

import httpx

from app.config import (  # noqa: F401 - re-exported
    DELTA_INDIA_PUBLIC_WS_URL, DELTA_INDIA_REST_URL, DELTA_INDIA_TESTNET_REST_URL, SUPPORTED_TIMEFRAMES,
)
from app.data.frames import floor_time, timeframe_delta
from app.data.source import DataSourceError, HistoricalCandleSource, MalformedResponseError, RateLimitedError
from app.models import Candle, to_utc
from app.monitoring.logging_setup import get_logger

log = get_logger(__name__)


# Documented REST resolutions (all of our supported timeframes appear in the list).
RESOLUTIONS = {tf: tf for tf in SUPPORTED_TIMEFRAMES}
MAX_CANDLES_PER_REQUEST = 2000
CHUNK_CANDLES = 1990  # margin below the documented cap in case ``end`` is inclusive
_MIN_PLAUSIBLE_EPOCH = 1_000_000_000  # 2001-09
_MAX_PLAUSIBLE_EPOCH = 100_000_000_000


class DeltaPublicClient(HistoricalCandleSource):
    def __init__(
        self,
        base_url: str = DELTA_INDIA_REST_URL,
        *,
        timeout_s: float = 10.0,
        max_retries: int = 3,
        backoff_s: float = 0.5,
        transport: httpx.BaseTransport | None = None,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
        user_agent: str = "cryptoquant-ai/0.1 (python-httpx)",
    ) -> None:
        self._client = httpx.Client(
            base_url=base_url,
            timeout=timeout_s,
            transport=transport,
            headers={"User-Agent": user_agent, "Accept": "application/json"},
        )
        self._max_retries = max_retries
        self._backoff_s = backoff_s
        self._sleep = sleep
        self._clock = clock

    def close(self) -> None:
        self._client.close()

    # ------------------------------------------------------------------ public
    def fetch_candles(self, symbol: str, timeframe: str, start: datetime, end: datetime) -> list[Candle]:
        if timeframe not in RESOLUTIONS:
            raise DataSourceError(f"unsupported timeframe {timeframe}")
        start, end, now = to_utc(start), to_utc(end), self._clock()
        step = timeframe_delta(timeframe)
        if floor_time(start, timeframe) != start or floor_time(end, timeframe) != end:
            raise DataSourceError("start and end must be aligned to the timeframe grid")
        collected: dict[datetime, Candle] = {}
        cursor = start
        while cursor < end:
            chunk_end = min(cursor + step * CHUNK_CANDLES, end)
            for candle in self._fetch_chunk(symbol, timeframe, cursor, chunk_end, now):
                if cursor <= candle.open_time < chunk_end and candle.is_complete(now):
                    collected[candle.open_time] = candle
            cursor = chunk_end
        return [collected[k] for k in sorted(collected)]

    # ----------------------------------------------------------------- internal
    def _fetch_chunk(self, symbol: str, timeframe: str, start: datetime, end: datetime, now: datetime) -> list[Candle]:
        params = {
            "resolution": RESOLUTIONS[timeframe],
            "symbol": symbol,
            "start": str(int(start.timestamp())),
            "end": str(int(end.timestamp())),
        }
        payload = self._get_json("/v2/history/candles", params)
        result = payload.get("result")
        if not isinstance(result, list):
            raise MalformedResponseError("'result' is not a list")
        if len(result) >= MAX_CANDLES_PER_REQUEST:
            raise MalformedResponseError(f"response hit the {MAX_CANDLES_PER_REQUEST}-candle cap; data may be truncated")
        return [self._parse_row(row, symbol, timeframe) for row in result]

    @staticmethod
    def _parse_row(row: Any, symbol: str, timeframe: str) -> Candle:
        try:
            t = int(row["time"])
            if not _MIN_PLAUSIBLE_EPOCH <= t < _MAX_PLAUSIBLE_EPOCH:
                raise ValueError(f"implausible epoch-seconds value {t}")
            open_time = datetime.fromtimestamp(t, tz=UTC)
            if floor_time(open_time, timeframe) != open_time:
                raise ValueError(f"time {t} not aligned to the {timeframe} UTC grid (unverified alignment assumption)")
            return Candle(
                symbol=symbol, timeframe=timeframe, open_time=open_time,
                open=float(row["open"]), high=float(row["high"]), low=float(row["low"]),
                close=float(row["close"]), volume=float(row["volume"]),
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise MalformedResponseError(f"bad candle row: {exc}") from exc

    def _get_json(self, path: str, params: dict[str, str]) -> dict[str, Any]:
        """GET with bounded retries. Retrying is safe here because these are idempotent public reads."""
        last_error: Exception | None = None
        for attempt in range(self._max_retries + 1):
            delay = self._backoff_s * (2**attempt)
            try:
                resp = self._client.get(path, params=params)
            except (httpx.TimeoutException, httpx.TransportError) as exc:
                last_error = exc
                log.warning("market-data request failed", extra={"attempt": attempt, "error": type(exc).__name__})
                if attempt < self._max_retries:
                    self._sleep(delay)
                continue
            if resp.status_code == 429:
                reset_ms = _parse_float(resp.headers.get("X-RATE-LIMIT-RESET"))
                wait = min(max(reset_ms / 1000.0, 0.1), 60.0) if reset_ms is not None else delay
                last_error = RateLimitedError("HTTP 429 from exchange")
                log.warning("rate limited", extra={"attempt": attempt, "wait_s": wait})
                if attempt < self._max_retries:
                    self._sleep(wait)
                continue
            if resp.status_code >= 500:
                last_error = DataSourceError(f"HTTP {resp.status_code}")
                if attempt < self._max_retries:
                    self._sleep(delay)
                continue
            if resp.status_code >= 400:
                raise DataSourceError(f"HTTP {resp.status_code} (not retried)")
            try:
                body = resp.json()
            except ValueError as exc:
                raise MalformedResponseError("response is not JSON") from exc
            if not isinstance(body, dict) or body.get("success") is not True:
                raise MalformedResponseError("response envelope has success != true")
            return body
        if isinstance(last_error, RateLimitedError):
            raise last_error
        raise DataSourceError(f"request failed after {self._max_retries + 1} attempts: {last_error!r}")


def _parse_float(value: str | None) -> float | None:
    try:
        return float(value) if value is not None else None
    except ValueError:
        return None


__all__ = ["DeltaPublicClient", "DELTA_INDIA_REST_URL", "DELTA_INDIA_TESTNET_REST_URL", "DELTA_INDIA_PUBLIC_WS_URL"]
