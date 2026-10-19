import asyncio
import json
from datetime import UTC, datetime, timedelta

import pytest

from app.data.live import (
    LiveCandleAssembler, LiveCandleFeed, MalformedMessageError, build_subscribe_message, connect_websocket,
    parse_candle_message,
)
from tests.conftest import T0

# Exact messages captured from wss://public-socket.india.delta.exchange on 19-Oct-2026.
REAL_ACK = '{"channels":[{"name":"candlestick_1m","symbols":["BTCUSD"]}],"type":"subscriptions"}'
REAL_CANDLE = ('{"c":82655.5,"cst":1792388880000000,"h":82660.0,"l":82655.0,"o":82659.5,"res":"1m","sy":"BTCUSD",'
               '"ts":1792388882342493,"type":"candlestick_1m","v":303.0}')


def msg(i=0, tf="1h", symbol="BTCUSD", close=100.0, **over):
    t = int((T0 + timedelta(hours=i)).timestamp())
    m = {"type": f"candlestick_{tf}", "res": tf, "sy": symbol, "cst": t * 10**6, "o": 100.0,
         "h": max(101.0, close + 1), "l": 99.0, "c": close, "v": 5.0, "ts": t * 10**6 + 123}
    m.update(over)
    return json.dumps(m)


def snap(i=0, close=100.0, tf="1h", symbol="BTCUSD"):
    return parse_candle_message(msg(i, tf, symbol, close=close))


# ------------------------------------------------------------------- parsing
def test_real_captured_candle_parses():
    c = parse_candle_message(REAL_CANDLE)
    assert (c.symbol, c.timeframe) == ("BTCUSD", "1m")
    assert c.open_time == datetime(2026, 10, 19, 5, 48, tzinfo=UTC)
    assert (c.open, c.high, c.low, c.close, c.volume) == (82659.5, 82660.0, 82655.0, 82655.5, 303.0)


def test_real_subscription_ack_is_ignored():
    assert parse_candle_message(REAL_ACK) is None


def test_non_candle_messages_are_ignored():
    for m in ('{"type":"heartbeat"}', "[1,2]", '{"no_type":1}'):
        assert parse_candle_message(m) is None


@pytest.mark.parametrize("bad_cst", [1704067200, 1704067200000, 1704067200 * 10**9])  # s, ms, ns
def test_non_microsecond_timestamps_are_rejected_not_reinterpreted(bad_cst):
    with pytest.raises(MalformedMessageError, match="microseconds"):
        parse_candle_message(msg(cst=bad_cst))


@pytest.mark.parametrize(
    "raw",
    [
        "not json",
        msg(cst=(int(T0.timestamp()) + 7) * 10**6),  # off-grid
        msg(tf="7m"),
        msg(res="15m"),  # res disagrees with channel
        msg(h=90),  # inconsistent OHLC
        msg(o="nan"),
        msg(cst="abc"),
        msg(cst=0),
        json.dumps({"type": "candlestick_1h", "sy": "BTCUSD"}),  # missing fields
        REAL_CANDLE.replace('"o":82659.5', '"o":-1'),
        # legacy long-form schema is deliberately unsupported
        json.dumps({"type": "candlestick_1h", "symbol": "BTCUSD", "candle_start_time": 1, "open": 1, "high": 1,
                    "low": 1, "close": 1, "volume": 1}),
    ],
)
def test_malformed_candle_messages_raise(raw):
    with pytest.raises(MalformedMessageError):
        parse_candle_message(raw)


def test_subscribe_message_shape():
    m = json.loads(build_subscribe_message(["BTCUSD", "ETHUSD"], ["15m", "1h"]))
    assert m == {"type": "subscribe", "payload": {"channels": [
        {"name": "candlestick_15m", "symbols": ["BTCUSD", "ETHUSD"]},
        {"name": "candlestick_1h", "symbols": ["BTCUSD", "ETHUSD"]}]}}


# ----------------------------------------------------------------- assembler
def test_assembler_finalizes_only_when_next_bar_starts_with_latest_values():
    a = LiveCandleAssembler("BTCUSD", "1h")
    assert a.on_update(snap(0, close=100)) == []
    assert a.on_update(snap(0, close=100.8)) == []  # same bar: replaced, not emitted
    out = a.on_update(snap(1, close=101))
    assert len(out) == 1 and out[0].open_time == T0 and out[0].close == 100.8
    assert a.on_update(snap(1, close=101.2)) == []  # new bar still in progress


def test_assembler_ignores_out_of_order_and_records_gaps():
    a = LiveCandleAssembler("BTCUSD", "1h")
    a.on_update(snap(2))
    assert a.on_update(snap(1)) == [] and a.ignored_out_of_order == 1
    out = a.on_update(snap(6))  # bars 3,4,5 never seen
    assert [c.open_time for c in out] == [T0 + timedelta(hours=2)]
    assert a.gaps == [(T0 + timedelta(hours=3), 3)]


def test_assembler_reset_discards_partial_bar_and_rejects_foreign_updates():
    a = LiveCandleAssembler("BTCUSD", "1h")
    a.on_update(snap(0))
    a.reset()
    assert a.on_update(snap(1)) == []  # bar 0 was partial; never emitted
    with pytest.raises(ValueError):
        a.on_update(snap(1, symbol="ETHUSD"))


# ---------------------------------------------------------------------- feed
class Boom(Exception):
    pass


class FakeConn:
    """Scripted connection. Items: str -> message, Exception -> raise, 'HANG' -> never returns, 'STOP' -> stop feed."""

    def __init__(self, script, feed_ref):
        self.script, self.feed_ref, self.closed = list(script), feed_ref, False

    async def recv(self):
        item = self.script.pop(0) if self.script else "STOP"
        if item == "HANG":
            await asyncio.Event().wait()
        if item == "STOP":
            self.feed_ref["feed"].stop()
            raise ConnectionError("closed")
        if isinstance(item, Exception):
            raise item
        return item

    async def close(self):
        self.closed = True


def run_feed(scripts, connect_errors=(), **kw):
    ref, emitted, sleeps, conns = {}, [], [], []
    scripts = list(scripts)

    async def connect():
        if connect_errors and len(conns) < len(connect_errors) and connect_errors[len(conns)]:
            conns.append(None)
            if len(conns) == len(connect_errors):
                ref["feed"].stop()
            raise OSError("refused")
        c = FakeConn(scripts.pop(0) if scripts else [], ref)
        conns.append(c)
        return c

    async def fake_sleep(s):
        sleeps.append(s)

    async def main():
        feed = LiveCandleFeed(connect, ["BTCUSD"], ["1h"], emitted.append, sleep=fake_sleep, **kw)
        ref["feed"] = feed
        await asyncio.wait_for(feed.run(), 5)
        return feed

    feed = asyncio.run(main())
    return feed, emitted, sleeps, conns


def test_feed_emits_only_finalized_candles():
    feed, emitted, _, conns = run_feed([[msg(0, close=100), msg(0, close=100.5), msg(1), "STOP"]])
    assert [(c.open_time, c.close) for c in emitted] == [(T0, 100.5)]
    assert conns[0].closed and not feed.connected


def test_reconnect_discards_partial_bar_flags_backfill_and_backs_off():
    feed, emitted, sleeps, _ = run_feed(
        [[msg(0), Boom("drop")], [msg(1), msg(2), "STOP"]], backoff_initial_s=1.0
    )
    assert [c.open_time for c in emitted] == [T0 + timedelta(hours=1)]  # bar 0 never emitted
    assert feed.reconnects == 1 and feed.needs_backfill and sleeps == [1.0]


def test_silence_timeout_triggers_reconnect():
    feed, _, sleeps, conns = run_feed([["HANG"], [msg(0), "STOP"]], silence_timeout_s=0.05)
    assert feed.silence_timeouts == 1 and feed.reconnects == 1 and conns[0].closed and len(sleeps) == 1


def test_connect_failures_back_off_exponentially_with_cap():
    feed, _, sleeps, _ = run_feed([], connect_errors=(True, True, True, True, True),
                                  backoff_initial_s=1.0, backoff_max_s=3.0)
    assert sleeps == [1.0, 2.0, 3.0, 3.0]


def test_bad_messages_callbacks_and_unsubscribed_symbols_do_not_kill_feed():
    ref, calls = {}, []

    def bad_callback(c):
        calls.append(c)
        raise RuntimeError("consumer bug")

    async def connect():
        return FakeConn(["garbage", msg(0, symbol="SOLUSD"), msg(0), msg(1), msg(2), "STOP"], ref)

    async def main():
        feed = LiveCandleFeed(connect, ["BTCUSD"], ["1h"], bad_callback, sleep=lambda s: asyncio.sleep(0))
        ref["feed"] = feed
        await asyncio.wait_for(feed.run(), 5)
        return feed

    feed = asyncio.run(main())
    assert feed.bad_messages == 1 and feed.unsubscribed_messages == 1
    assert len(calls) == 2 and feed.callback_errors == 2


def test_freshness_requires_connection_and_recent_message():
    now = datetime(2024, 1, 1, tzinfo=UTC)
    feed = LiveCandleFeed(lambda: None, ["BTCUSD"], ["1h"], lambda c: None, silence_timeout_s=60, clock=lambda: now)
    assert not feed.is_fresh()
    feed.connected, feed.last_message_at = True, now - timedelta(seconds=59)
    assert feed.is_fresh()
    feed.last_message_at = now - timedelta(seconds=61)
    assert not feed.is_fresh()
    feed.connected, feed.last_message_at = False, now
    assert not feed.is_fresh()


# --------------------------------------------- real WebSocket on localhost
def test_real_websocket_subscribe_and_stream():
    from websockets.asyncio.server import serve

    received, emitted = [], []

    async def handler(ws):
        received.append(await ws.recv())
        await ws.send(json.dumps({"type": "subscriptions"}))  # ack-like noise
        await ws.send(msg(0, close=100.5))
        await ws.send(msg(1))
        # returning closes the connection

    async def main():
        async with serve(handler, "127.0.0.1", 0) as server:
            port = server.sockets[0].getsockname()[1]

            async def connect():
                return await connect_websocket(f"ws://127.0.0.1:{port}", ["BTCUSD"], ["1h"])

            feed = LiveCandleFeed(connect, ["BTCUSD"], ["1h"], lambda c: (emitted.append(c), feed.stop()),
                                  sleep=lambda s: asyncio.sleep(0))
            await asyncio.wait_for(feed.run(), 10)
            return feed

    asyncio.run(main())
    assert json.loads(received[0])["payload"]["channels"][0]["name"] == "candlestick_1h"
    assert [(c.open_time, c.close) for c in emitted] == [(T0, 100.5)]
