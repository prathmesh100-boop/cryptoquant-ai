"""Live candle ingestion over WebSocket.

Design rules
------------
* An in-progress candle is NEVER emitted. A bar is finalized only when an update for a newer bar arrives.
* Finalized WebSocket candles are PROVISIONAL (the last in-bar update may have been missed). Store them with
  ``source="ws"`` and run ``HistoryDownloader.reconcile_recent`` to overwrite them with REST values.
* After any (re)connect, partial bars are discarded and ``needs_backfill`` is set so history can be re-synced.
* If no message arrives within ``silence_timeout_s`` the feed marks itself stale and reconnects.
  ``is_fresh()`` must be checked before any signal is generated.

Message schema (VERIFIED from a live capture of wss://public-socket.india.delta.exchange, 19 Oct 2026)::

    {"type":"candlestick_1m","res":"1m","sy":"BTCUSD","cst":1792388880000000,
     "o":82659.5,"h":82660.0,"l":82655.0,"c":82655.5,"v":303.0,"ts":1792388882342493}

``cst`` (candle start) and ``ts`` are epoch MICROseconds; ``v`` is in contracts. The parser is strict: any other
unit, a ``res`` that disagrees with the channel name, an off-grid start, or an inconsistent OHLC is rejected
rather than guessed at. The long-form legacy schema (``candle_start_time``/``open``/...) is intentionally not
supported; that endpoint was scheduled for removal on 31-Jul-2026.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Awaitable, Callable, Sequence
from datetime import UTC, datetime
from typing import Any, Protocol

from pydantic import ValidationError

from app.config import SUPPORTED_TIMEFRAMES
from app.data.frames import floor_time, timeframe_delta
from app.models import Candle
from app.monitoring.logging_setup import get_logger

log = get_logger(__name__)


class MalformedMessageError(ValueError):
    """A candle message violated the expected schema or an invariant."""


def build_subscribe_message(symbols: Sequence[str], timeframes: Sequence[str]) -> str:
    """Subscribe payload shape as documented for Delta's ``candlestick_<resolution>`` channels."""
    channels = [{"name": f"candlestick_{tf}", "symbols": list(symbols)} for tf in timeframes]
    return json.dumps({"type": "subscribe", "payload": {"channels": channels}})


def _micros_to_datetime(value: Any) -> datetime:
    """Epoch MICROseconds only (verified unit). Other magnitudes are rejected, never reinterpreted."""
    try:
        n = int(value)
    except (TypeError, ValueError) as exc:
        raise MalformedMessageError(f"non-numeric timestamp {value!r}") from exc
    if not 10**15 <= n < 10**16:  # 2001..2286 expressed in microseconds
        raise MalformedMessageError(f"timestamp {n} is not epoch microseconds")
    return datetime.fromtimestamp(n // 10**6, tz=UTC)


def parse_candle_message(raw: str | bytes | dict[str, Any]) -> Candle | None:
    """Return an in-progress ``Candle`` snapshot, or ``None`` for non-candle messages (acks, heartbeats)."""
    try:
        msg = json.loads(raw) if isinstance(raw, (str, bytes)) else raw
    except ValueError as exc:
        raise MalformedMessageError("message is not valid JSON") from exc
    if not isinstance(msg, dict):
        return None
    mtype = msg.get("type")
    if not isinstance(mtype, str) or not mtype.startswith("candlestick_"):
        return None
    timeframe = mtype.removeprefix("candlestick_")
    if timeframe not in SUPPORTED_TIMEFRAMES:
        raise MalformedMessageError(f"unsupported candle resolution {timeframe!r}")
    try:
        res = msg.get("res")
        if res is not None and res != timeframe:
            raise MalformedMessageError(f"res {res!r} disagrees with channel {mtype!r}")
        open_time = _micros_to_datetime(msg["cst"])
        if floor_time(open_time, timeframe) != open_time:
            raise MalformedMessageError(f"cst {open_time} not aligned to the {timeframe} grid")
        return Candle(
            symbol=str(msg["sy"]), timeframe=timeframe, open_time=open_time,
            open=float(msg["o"]), high=float(msg["h"]), low=float(msg["l"]),
            close=float(msg["c"]), volume=float(msg["v"]),
        )
    except (KeyError, TypeError, ValueError, ValidationError) as exc:
        if isinstance(exc, MalformedMessageError):
            raise
        raise MalformedMessageError(f"bad candle message: {exc!r}") from exc


class LiveCandleAssembler:
    """Turns a stream of in-progress snapshots for ONE (symbol, timeframe) into finalized candles."""

    def __init__(self, symbol: str, timeframe: str) -> None:
        self.symbol, self.timeframe = symbol, timeframe
        self.current: Candle | None = None
        self.ignored_out_of_order = 0
        self.gaps: list[tuple[datetime, int]] = []  # (first missing open_time, bars missing)

    def reset(self) -> None:
        """Discard the partial bar (e.g. after a reconnect). It is never emitted."""
        self.current = None

    def on_update(self, update: Candle) -> list[Candle]:
        if (update.symbol, update.timeframe) != (self.symbol, self.timeframe):
            raise ValueError("update does not belong to this assembler")
        cur = self.current
        if cur is None:
            self.current = update
            return []
        if update.open_time < cur.open_time:
            self.ignored_out_of_order += 1
            return []
        if update.open_time == cur.open_time:
            self.current = update  # latest snapshot of the in-progress bar wins
            return []
        expected_next = cur.open_time + timeframe_delta(self.timeframe)
        if update.open_time > expected_next:
            missing = int((update.open_time - expected_next) / timeframe_delta(self.timeframe))
            self.gaps.append((expected_next, missing))
            log.warning("live feed skipped bars", extra={"symbol": self.symbol, "missing": missing})
        self.current = update
        return [cur]


class Connection(Protocol):
    async def recv(self) -> str | bytes: ...
    async def close(self) -> None: ...


class LiveCandleFeed:
    def __init__(
        self,
        connect: Callable[[], Awaitable[Connection]],
        symbols: Sequence[str],
        timeframes: Sequence[str],
        on_candle: Callable[[Candle], None],
        *,
        silence_timeout_s: float = 60.0,
        backoff_initial_s: float = 1.0,
        backoff_max_s: float = 30.0,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._connect, self._on_candle = connect, on_candle
        self._assemblers = {(s, tf): LiveCandleAssembler(s, tf) for s in symbols for tf in timeframes}
        self._silence_timeout_s = silence_timeout_s
        self._backoff_initial_s, self._backoff_max_s = backoff_initial_s, backoff_max_s
        self._sleep, self._clock = sleep, clock
        self._stop = asyncio.Event()
        self.connected = False
        self.last_message_at: datetime | None = None
        self.reconnects = 0
        self.silence_timeouts = 0
        self.bad_messages = 0
        self.callback_errors = 0
        self.unsubscribed_messages = 0
        self.needs_backfill = False

    @property
    def assemblers(self) -> dict[tuple[str, str], LiveCandleAssembler]:
        return self._assemblers

    def stop(self) -> None:
        self._stop.set()

    def is_fresh(self, now: datetime | None = None) -> bool:
        """True only if connected and a message arrived within the silence window."""
        if not self.connected or self.last_message_at is None:
            return False
        age = ((now or self._clock()) - self.last_message_at).total_seconds()
        return age <= self._silence_timeout_s

    async def run(self) -> None:
        backoff = self._backoff_initial_s
        while not self._stop.is_set():
            conn: Connection | None = None
            try:
                conn = await self._connect()
                self.connected = True
                for a in self._assemblers.values():
                    a.reset()
                log.info("live feed connected")
                while not self._stop.is_set():
                    raw = await asyncio.wait_for(conn.recv(), self._silence_timeout_s)
                    backoff = self._backoff_initial_s
                    self._handle(raw)
            except asyncio.TimeoutError:
                self.silence_timeouts += 1
                log.warning("live feed silent; reconnecting", extra={"timeout_s": self._silence_timeout_s})
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # connection errors of any library: reconnect, never crash the loop
                log.warning("live feed error; reconnecting", extra={"error": type(exc).__name__})
            finally:
                self.connected = False
                if conn is not None:
                    try:
                        await conn.close()
                    except Exception:  # noqa: BLE001 - best-effort close
                        pass
            if self._stop.is_set():
                break
            self.reconnects += 1
            self.needs_backfill = True
            await self._sleep(backoff)
            backoff = min(backoff * 2, self._backoff_max_s)

    def _handle(self, raw: str | bytes) -> None:
        self.last_message_at = self._clock()  # any message proves liveness (incl. heartbeats/acks)
        try:
            update = parse_candle_message(raw)
        except MalformedMessageError as exc:
            self.bad_messages += 1
            log.warning("bad live message dropped", extra={"error": str(exc)})
            return
        if update is None:
            return
        assembler = self._assemblers.get((update.symbol, update.timeframe))
        if assembler is None:
            self.unsubscribed_messages += 1
            return
        for candle in assembler.on_update(update):
            try:
                self._on_candle(candle)
            except Exception:  # noqa: BLE001 - a callback bug must not kill the feed
                self.callback_errors += 1
                log.exception("on_candle callback failed")


async def connect_websocket(url: str, symbols: Sequence[str], timeframes: Sequence[str], *, open_timeout: float = 10.0) -> Connection:
    """Open a real WebSocket and send the subscribe message. The library's built-in ping/pong keepalive stays on."""
    from websockets.asyncio.client import connect  # lazy: keeps tests/offline use import-light

    ws = await connect(url, open_timeout=open_timeout)
    await ws.send(build_subscribe_message(symbols, timeframes))
    return ws  # type: ignore[return-value]
