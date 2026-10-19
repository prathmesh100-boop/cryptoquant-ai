from datetime import timedelta

import pytest

from app.data.downloader import HistoryDownloader
from app.data.frames import FrameError
from app.data.replay import ReplayFeed
from app.data.source import CsvCandleSource, HistoricalCandleSource, MalformedResponseError
from app.storage.candle_store import SqliteCandleStore, StorageError
from tests.conftest import T0, make_candles, make_frame


class FakeSource(HistoricalCandleSource):
    def __init__(self, candles, honour_range=True):
        self.data = {c.open_time: c for c in candles}
        self.calls, self.honour_range = [], honour_range

    def fetch_candles(self, symbol, timeframe, start, end):
        self.calls.append((start, end))
        if not self.honour_range:
            return sorted(self.data.values(), key=lambda c: c.open_time)
        return [c for t, c in sorted(self.data.items()) if start <= t < end]


def hours(n):
    return T0 + timedelta(hours=n)


def setup(candles, now, **kw):
    clock = {"now": now}
    src, store = FakeSource(candles, **kw), SqliteCandleStore()
    return src, store, clock, HistoryDownloader(src, store, clock=lambda: clock["now"])


def test_initial_sync_excludes_in_progress_bar():
    src, store, _, dl = setup(make_candles(12), hours(10) + timedelta(minutes=30))
    rep = dl.sync("BTCUSD", "1h", T0)
    assert (rep.fetched, rep.stored_total, rep.is_contiguous) == (10, 10, True)
    assert store.latest_open_time("BTCUSD", "1h") == hours(9)  # bar opening at 10:00 still in progress


def test_resume_fetches_only_new_data_and_is_idempotent():
    src, store, clock, dl = setup(make_candles(20), hours(10) + timedelta(minutes=30))
    dl.sync("BTCUSD", "1h", T0)
    clock["now"] = hours(14) + timedelta(minutes=10)
    rep = dl.sync("BTCUSD", "1h", T0)
    assert src.calls[-1][0] == hours(10) and rep.resumed_after == hours(9)
    assert (rep.fetched, rep.stored_total) == (4, 14)
    n_calls = len(src.calls)
    again = dl.sync("BTCUSD", "1h", T0)
    assert len(src.calls) == n_calls and again.fetched == 0 and again.stored_total == 14


def test_gap_is_refilled_once_and_unfillable_gap_is_reported_not_faked():
    candles = [c for c in make_candles(10) if c.open_time != hours(5)]
    src, store, clock, dl = setup(candles, hours(10))
    rep = dl.sync("BTCUSD", "1h", T0)
    assert not rep.is_contiguous and rep.remaining_gaps[0].first_missing == hours(5)
    assert (hours(5), hours(6)) in src.calls  # refill attempt was made
    assert store.count("BTCUSD", "1h") == 9  # nothing invented
    src.data[hours(5)] = make_candles(10)[5]  # exchange now has it
    assert dl.sync("BTCUSD", "1h", T0).is_contiguous and store.count("BTCUSD", "1h") == 10


def test_misbehaving_source_cannot_store_incomplete_candle():
    src, store, _, dl = setup(make_candles(12), hours(10) + timedelta(minutes=30), honour_range=False)
    with pytest.raises(StorageError, match="incomplete"):
        dl.sync("BTCUSD", "1h", T0)
    assert store.count("BTCUSD", "1h") == 0


def test_reconcile_overwrites_provisional_ws_candles_with_rest_values():
    good = make_candles(10)
    src, store, _, dl = setup(good, hours(10))
    store.upsert_candles([c.model_copy(update={"close": c.close + 0.7, "high": c.high + 0.7}) for c in good[-5:]],
                         as_of=hours(10), source="ws")
    assert dl.reconcile_recent("BTCUSD", "1h", bars=5) == 5
    assert list(store.load_frame("BTCUSD", "1h")["close"]) == [c.close for c in good[-5:]]
    assert dl.reconcile_recent("BTCUSD", "1h", bars=5) == 0  # nothing left to correct


def test_replay_exposes_only_knowable_data():
    f = make_frame(30)
    steps = list(ReplayFeed(f, "1h").steps(warmup=10))
    assert len(steps) == 21 and len(steps[0].window) == 10
    for i, s in enumerate(steps):
        assert len(s.window) == 10 + i
        assert s.as_of == s.window.index[-1].to_pydatetime() + timedelta(hours=1)
        assert (s.window.index + timedelta(hours=1) <= s.as_of).all()  # every bar closed by as_of


def test_replay_rejects_frames_with_gaps():
    f = make_frame(30)
    with pytest.raises(FrameError, match="missing candles"):
        ReplayFeed(f.drop(f.index[10]), "1h")


def test_csv_source(tmp_path):
    p = tmp_path / "c.csv"
    p.write_text("open_time,open,high,low,close,volume\n"
                 "1704067200,100,101,99,100.5,3\n"
                 "2024-01-01T01:00:00Z,100.5,102,100,101,4\n"
                 "1704074400,101,103,100,102,5\n")
    out = CsvCandleSource(p).fetch_candles("BTCUSD", "1h", T0, hours(2))
    assert [c.close for c in out] == [100.5, 101]  # [start, end) respected, ISO + epoch both parsed


def test_csv_source_rejects_bad_files(tmp_path):
    p = tmp_path / "bad.csv"
    p.write_text("open_time,open,high\n1,2,3\n")
    with pytest.raises(MalformedResponseError, match="missing columns"):
        CsvCandleSource(p).fetch_candles("BTCUSD", "1h", T0, hours(2))
    p.write_text("open_time,open,high,low,close,volume\n2024-01-01T00:00:00,1,2,1,2,1\n")
    with pytest.raises(MalformedResponseError):  # naive timestamp
        CsvCandleSource(p).fetch_candles("BTCUSD", "1h", T0, hours(2))
