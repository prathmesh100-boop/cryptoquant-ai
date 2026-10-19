"""Historical data synchronisation: resume, gap refill, and REST reconciliation."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime

from app.data.frames import floor_time, timeframe_delta
from app.data.quality import Gap, find_gaps
from app.data.source import HistoricalCandleSource
from app.models import to_utc
from app.monitoring.logging_setup import get_logger
from app.storage.candle_store import CandleRepository

log = get_logger(__name__)


@dataclass
class SyncReport:
    symbol: str
    timeframe: str
    range_start: datetime
    range_end: datetime
    fetched: int = 0
    stored_total: int = 0
    resumed_after: datetime | None = None
    remaining_gaps: list[Gap] = field(default_factory=list)

    @property
    def is_contiguous(self) -> bool:
        return not self.remaining_gaps


class HistoryDownloader:
    def __init__(
        self,
        source: HistoricalCandleSource,
        store: CandleRepository,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._source, self._store, self._clock = source, store, clock

    def sync(self, symbol: str, timeframe: str, start: datetime, end: datetime | None = None) -> SyncReport:
        """Download ``[start, end)`` (default: up to the latest COMPLETE bar), resuming after stored data.

        Remaining gaps after one refill attempt are reported, never silently filled or interpolated.
        """
        now = to_utc(self._clock())
        start = floor_time(start, timeframe)
        end = floor_time(end or now, timeframe)  # open time of the in-progress bar: excluded
        report = SyncReport(symbol, timeframe, start, end)
        step = timeframe_delta(timeframe)

        latest = self._store.latest_open_time(symbol, timeframe)
        begin = start
        if latest is not None and latest >= start:
            report.resumed_after = latest
            begin = latest + step
        if begin < end:
            candles = self._source.fetch_candles(symbol, timeframe, begin, end)
            self._store.upsert_candles(candles, as_of=now)
            report.fetched += len(candles)

        gaps = find_gaps(self._store.load_frame(symbol, timeframe, start, end), timeframe)
        for gap in gaps:  # one refill attempt per gap; exchanges can have genuine no-trade gaps
            refill = self._source.fetch_candles(symbol, timeframe, gap.first_missing, gap.last_missing + step)
            self._store.upsert_candles(refill, as_of=now)
            report.fetched += len(refill)
        frame = self._store.load_frame(symbol, timeframe, start, end)
        report.remaining_gaps = find_gaps(frame, timeframe)
        report.stored_total = len(frame)
        log.info(
            "history synced",
            extra={"symbol": symbol, "timeframe": timeframe, "fetched": report.fetched,
                   "stored": report.stored_total, "gaps": len(report.remaining_gaps)},
        )
        return report

    def reconcile_recent(self, symbol: str, timeframe: str, bars: int = 5) -> int:
        """Re-fetch the last ``bars`` complete bars from REST (authoritative) and overwrite stored values.

        Returns the number of stored candles whose values changed. Use after live/WebSocket ingestion,
        whose final in-bar update may have been missed.
        """
        now = to_utc(self._clock())
        end = floor_time(now, timeframe)
        start = end - timeframe_delta(timeframe) * bars
        before = self._store.load_frame(symbol, timeframe, start, end)
        candles = self._source.fetch_candles(symbol, timeframe, start, end)
        self._store.upsert_candles(candles, as_of=now, source="rest")
        after = self._store.load_frame(symbol, timeframe, start, end)
        changed = 0
        for ts in before.index.intersection(after.index):
            if not before.loc[ts].equals(after.loc[ts]):
                changed += 1
        return changed
