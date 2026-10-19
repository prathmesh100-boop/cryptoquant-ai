from datetime import timedelta

import httpx
import pytest

from app.config import SUPPORTED_TIMEFRAMES
from app.data.delta_rest import DeltaPublicClient
from app.data.source import DataSourceError, MalformedResponseError, RateLimitedError
from tests.conftest import T0

NOW = T0 + timedelta(days=60)


def rows(timeframe, start_s, end_s, inclusive_end=True):
    step = SUPPORTED_TIMEFRAMES[timeframe]
    t = start_s
    out = []
    while t < end_s or (inclusive_end and t == end_s):
        i = (t - int(T0.timestamp())) // step
        out.append({"time": t, "open": 100 + i - 0.5, "high": 101 + i, "low": 99 + i, "close": 100 + i, "volume": 5})
        t += step
    return out


class Server:
    """Programmable fake exchange. ``script`` entries are consumed per request before falling back to normal service."""

    def __init__(self, script=None, inclusive_end=True):
        self.calls, self.script, self.inclusive_end = [], list(script or []), inclusive_end

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.calls.append(request)
        if self.script:
            action = self.script.pop(0)
            if isinstance(action, Exception):
                raise action
            if action is not None:
                return action
        q = request.url.params
        body = {"success": True, "result": rows(q["resolution"], int(q["start"]), int(q["end"]), self.inclusive_end)}
        return httpx.Response(200, json=body)


def client(server, **kw):
    sleeps = []
    c = DeltaPublicClient("https://example.test", transport=httpx.MockTransport(server),
                          sleep=sleeps.append, clock=lambda: NOW, **kw)
    c.sleeps = sleeps
    return c


def test_request_shape_and_headers():
    s = Server()
    c = client(s)
    out = c.fetch_candles("BTCUSD", "1h", T0, T0 + timedelta(hours=5))
    req = s.calls[0]
    assert req.url.path == "/v2/history/candles"
    assert dict(req.url.params) == {"resolution": "1h", "symbol": "BTCUSD",
                                    "start": str(int(T0.timestamp())), "end": str(int((T0 + timedelta(hours=5)).timestamp()))}
    assert req.headers["user-agent"].startswith("cryptoquant-ai")  # mandatory per Delta docs
    assert [x.close for x in out] == [100, 101, 102, 103, 104]


def test_inclusive_end_does_not_leak_extra_candle_or_duplicates():
    out = client(Server(inclusive_end=True)).fetch_candles("BTCUSD", "1h", T0, T0 + timedelta(hours=5))
    assert len(out) == 5 and out[-1].open_time == T0 + timedelta(hours=4)


def test_chunking_respects_request_cap_and_returns_contiguous_unique_data():
    s = Server()
    out = client(s).fetch_candles("BTCUSD", "1m", T0, T0 + timedelta(minutes=5000))
    assert len(s.calls) == 3
    assert len(out) == 5000 and len({c.open_time for c in out}) == 5000
    assert all(len(rows("1m", int(r.url.params["start"]), int(r.url.params["end"]))) < 2000 for r in s.calls)


def test_incomplete_current_candle_is_dropped():
    c = DeltaPublicClient("https://example.test", transport=httpx.MockTransport(Server()),
                          clock=lambda: T0 + timedelta(hours=2, minutes=30), sleep=lambda s: None)
    out = c.fetch_candles("BTCUSD", "1h", T0, T0 + timedelta(hours=5))
    assert [x.open_time for x in out] == [T0, T0 + timedelta(hours=1)]  # bar 2 closes at 03:00


def test_empty_result_is_valid():
    s = Server(script=[httpx.Response(200, json={"success": True, "result": []})])
    assert client(s).fetch_candles("BTCUSD", "1h", T0, T0 + timedelta(hours=2)) == []


def test_rate_limit_waits_for_header_then_succeeds():
    s = Server(script=[httpx.Response(429, headers={"X-RATE-LIMIT-RESET": "1500"})])
    c = client(s)
    assert len(c.fetch_candles("BTCUSD", "1h", T0, T0 + timedelta(hours=2))) == 2
    assert c.sleeps == [1.5] and len(s.calls) == 2


def test_rate_limit_wait_is_clamped():
    s = Server(script=[httpx.Response(429, headers={"X-RATE-LIMIT-RESET": "9999999"})])
    c = client(s)
    c.fetch_candles("BTCUSD", "1h", T0, T0 + timedelta(hours=1))
    assert c.sleeps == [60.0]


def test_persistent_rate_limit_raises_after_bounded_retries():
    s = Server(script=[httpx.Response(429)] * 10)
    c = client(s, max_retries=2)
    with pytest.raises(RateLimitedError):
        c.fetch_candles("BTCUSD", "1h", T0, T0 + timedelta(hours=1))
    assert len(s.calls) == 3 and len(c.sleeps) == 2


def test_timeout_is_retried_with_backoff_then_succeeds():
    s = Server(script=[httpx.ReadTimeout("slow"), httpx.ConnectError("down")])
    c = client(s, backoff_s=0.5)
    assert len(c.fetch_candles("BTCUSD", "1h", T0, T0 + timedelta(hours=2))) == 2
    assert c.sleeps == [0.5, 1.0]


def test_timeouts_exhausted_raise_data_source_error():
    c = client(Server(script=[httpx.ReadTimeout("x")] * 10), max_retries=1)
    with pytest.raises(DataSourceError, match="failed after 2 attempts") as ei:
        c.fetch_candles("BTCUSD", "1h", T0, T0 + timedelta(hours=1))
    assert not isinstance(ei.value, RateLimitedError)


def test_server_errors_retried_but_client_errors_are_not():
    s = Server(script=[httpx.Response(503)])
    assert len(client(s).fetch_candles("BTCUSD", "1h", T0, T0 + timedelta(hours=2))) == 2
    s = Server(script=[httpx.Response(400, json={"success": False, "error": {"code": "bad"}})])
    with pytest.raises(DataSourceError, match="400"):
        client(s).fetch_candles("BTCUSD", "1h", T0, T0 + timedelta(hours=1))
    assert len(s.calls) == 1


@pytest.mark.parametrize(
    "response",
    [
        httpx.Response(200, content=b"<html>blocked</html>"),
        httpx.Response(200, json={"success": False, "error": {"code": "x"}}),
        httpx.Response(200, json={"success": True, "result": {"not": "a list"}}),
        httpx.Response(200, json=["bare list"]),
        httpx.Response(200, json={"success": True, "result": [{"time": 1704067200, "open": 1}]}),  # missing keys
        httpx.Response(200, json={"success": True, "result": [  # not on the 1h grid
            {"time": 1704067200 + 7, "open": 1, "high": 2, "low": 1, "close": 2, "volume": 1}]}),
        httpx.Response(200, json={"success": True, "result": [  # high < low
            {"time": 1704067200, "open": 5, "high": 4, "low": 6, "close": 5, "volume": 1}]}),
        httpx.Response(200, json={"success": True, "result": [  # milliseconds where seconds expected
            {"time": 1704067200000, "open": 1, "high": 2, "low": 1, "close": 2, "volume": 1}]}),
        httpx.Response(200, json={"success": True, "result": [
            {"time": 1704067200, "open": "NaN", "high": 2, "low": 1, "close": 2, "volume": 1}]}),
    ],
)
def test_malformed_payloads_raise_and_are_never_repaired(response):
    with pytest.raises(MalformedResponseError):
        client(Server(script=[response])).fetch_candles("BTCUSD", "1h", T0, T0 + timedelta(hours=1))


def test_truncation_at_cap_is_detected():
    big = [{"time": 1704067200 + 3600 * i, "open": 1, "high": 2, "low": 1, "close": 1, "volume": 1} for i in range(2000)]
    s = Server(script=[httpx.Response(200, json={"success": True, "result": big})])
    with pytest.raises(MalformedResponseError, match="cap"):
        client(s).fetch_candles("BTCUSD", "1h", T0, T0 + timedelta(hours=10))


def test_argument_validation():
    c = client(Server())
    with pytest.raises(DataSourceError, match="unsupported"):
        c.fetch_candles("BTCUSD", "7m", T0, T0 + timedelta(hours=1))
    with pytest.raises(DataSourceError, match="aligned"):
        c.fetch_candles("BTCUSD", "1h", T0 + timedelta(minutes=5), T0 + timedelta(hours=3))
    with pytest.raises(ValueError, match="naive"):
        c.fetch_candles("BTCUSD", "1h", T0.replace(tzinfo=None), T0 + timedelta(hours=3))
